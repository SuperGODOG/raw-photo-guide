#!/usr/bin/env python3
"""Local JSON API over RawTherapee CLI; no server, no arbitrary CLI arguments."""
import argparse
import configparser
from contextlib import nullcontext, ExitStack
import copy
from datetime import datetime, timezone
import fcntl
import errno
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time

VERSION = '0.6.7'
MIN_RENDER_FREE_BYTES = 500 * 1024 * 1024
RENDER_TIMEOUT_RULE = {'type': 'integer', 'minimum': 30, 'maximum': 3600, 'default': 300}
BUNDLED_PROFILES = {
    'builtin:neutral-rec2020-rt513': Path(__file__).resolve().parents[1] / 'assets/neutral-rec2020-rt513.pp3',
    'builtin:lcp:viltrox-af-50f2-z': Path(__file__).resolve().parents[1] / 'assets/lens_profiles/viltrox-af-50f2-z.lcp',
}
BUNDLED_LCP_METADATA = {
    'builtin:lcp:viltrox-af-50f2-z': {
        'calibration_status': 'unverified',
        'recommended': False,
        'usage': 'explicit_experiment_only',
        'provenance': 'Calibration source, targets and error measurements have not been supplied.',
        'expected_sha256': '2b649897c2a7766a10856332d7154136f3349afffa6c28cb3a9d86e986d8bc78'
    }
}
BUILTIN_LENS_MAP = {
    'viltrox af 50/2.0 z': 'builtin:lcp:viltrox-af-50f2-z',
    'viltrox 50mm f/2': 'builtin:lcp:viltrox-af-50f2-z',
    'viltrox af 50mm f/2 z': 'builtin:lcp:viltrox-af-50f2-z',
}
RAW_EXTENSIONS = {'.nef', '.nrw', '.dng', '.arw', '.cr2', '.cr3', '.raf', '.orf', '.rw2', '.pef'}
JPEG_EXTENSIONS = {'.jpg', '.jpeg'}

def check_nef_header(source):
    try:
        with open(source, 'rb') as f:
            head = f.read(4)
    except Exception as e:
        raise ToolError('CORRUPT_INPUT', f'Cannot read NEF file: {e}')
    require(len(head) == 4, 'CORRUPT_INPUT', f'File is too short to be a valid NEF/TIFF container ({len(head)} bytes)')
    if head not in (b'II\x2a\x00', b'MM\x00\x2a'):
        raise ToolError('UNSUPPORTED_INPUT',
                        f"File extension is .nef but TIFF container header magic is invalid ({head!r}); "
                        f"masqueraded or invalid files rejected. "
                        f"Note: TIFF header check confirms container magic only, not full RAW decode validity.")
    return {
        'container': 'tiff',
        'byte_order': 'little' if head[:2] == b'II' else 'big',
        'container_check': 'tiff_header_valid',
        'decode_disclaimer': 'TIFF container header check confirms container magic only, not full RAW decode validity; actual validity depends on engine/decoder processing.'
    }

def check_source(source):
    ext = source.suffix.lower()
    if ext in RAW_EXTENSIONS:
        res = {'source_type': 'raw'}
        if ext == '.nef':
            hdr = check_nef_header(source)
            res.update(hdr)
        return res
    if ext in JPEG_EXTENSIONS:
        require(importlib.util.find_spec('PIL') is not None, 'DEPENDENCY_MISSING', 'Pillow is required for JPEG input validation')
        from PIL import Image, ImageCms
        try:
            with Image.open(source) as im:
                if im.format not in ('JPEG', 'MPO'):
                    raise ToolError('UNSUPPORTED_INPUT', f'File extension is {source.suffix} but detected format is {im.format}; masqueraded files rejected')
                im.load()
                icc = im.info.get('icc_profile')
                desc = None
                if icc:
                    try:
                        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
                    except Exception:
                        desc = 'unknown'
                exif = im.getexif()
                orientation = exif.get(0x0112) if exif else None
                return {
                    'source_type': 'jpeg',
                    'has_icc': bool(icc),
                    'icc_description': desc,
                    'orientation': orientation
                }
        except ToolError:
            raise
        except Exception as e:
            raise ToolError('CORRUPT_INPUT', f'Cannot decode JPEG image: {e}')
    raise ToolError('UNSUPPORTED_INPUT', f'Unsupported input extension: {source.suffix}; accepts RAW or JPEG (.jpg/.jpeg)')

STR = {'type': 'string', 'minLength': 1}

VARIANT_DOCTOR = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['doctor']},
    },
    'required': ['action'],
    'additionalProperties': False,
}

VARIANT_CAPABILITIES = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['capabilities']},
        'source_type': {'type': 'string', 'enum': ['raw', 'jpeg']},
        'detail': {'type': 'string', 'enum': ['summary', 'full']},
        'parameters': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'type': 'string'}},
    },
    'required': ['action'],
    'additionalProperties': False,
}

VARIANT_INSPECT = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['inspect']},
        'source': STR,
        'lens_check': {'type': 'boolean'},
    },
    'required': ['action', 'source'],
    'additionalProperties': False,
}

VARIANT_PROFILE = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['profile']},
        'profile': STR,
        'kind': {'type': 'string', 'enum': ['pp3', 'dcp', 'icc', 'lcp'], 'description': 'Defaults to pp3. ICC/ICM files require kind=icc; ICC parsing does not prove RGB output compatibility.'},
    },
    'required': ['action', 'profile'],
    'additionalProperties': False,
}

VARIANT_QC = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['qc']},
        'image': STR,
    },
    'required': ['action', 'image'],
    'additionalProperties': False,
}

VARIANT_RAW_QC = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['raw_qc']},
        'source': STR,
        'roi': {
            'type': 'object',
            'properties': {
                'x': {'type': 'integer', 'minimum': 0},
                'y': {'type': 'integer', 'minimum': 0},
                'width': {'type': 'integer', 'minimum': 1},
                'height': {'type': 'integer', 'minimum': 1},
            },
            'required': ['x', 'y', 'width', 'height'],
            'additionalProperties': False,
        },
    },
    'required': ['action', 'source'],
    'additionalProperties': False,
}

VARIANT_RENDER = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['render']},
        'source': STR,
        'profile': STR,
        'profile_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'engine_version': STR,
        'output_dir': STR,
        'format': {'type': 'string', 'enum': ['jpeg', 'tiff16']},
        'timeout_seconds': RENDER_TIMEOUT_RULE,
        'output_profile': {'type': 'object', 'properties': {
            'path': STR, 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
            'required': ['path', 'sha256'], 'additionalProperties': False},
        'preview_metadata': {'type': 'string', 'enum': ['strip', 'capture', 'capture_camera'], 'default': 'strip'},
        'threads': {'type': 'integer', 'minimum': 1, 'maximum': 256},
        'exposure_ev': {'type': 'number', 'minimum': -2, 'maximum': 2},
        'adjustments': {'type': 'object'},
        'session_path': STR,
        'expected_revision': {'type': 'integer', 'minimum': 0},
        'reset_adjustments': {'type': 'boolean'},
        'reset_fields': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['action', 'source', 'profile', 'profile_sha256', 'engine_version', 'output_dir', 'format'],
    'additionalProperties': False,
}

VARIANT_PRUNE_HISTORY = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['prune_history']},
        'session_path': STR,
        'expected_revision': {'type': 'integer', 'minimum': 0},
        'manifest_paths': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': STR},
        'protect_session_paths': {'type': 'array', 'uniqueItems': True, 'items': STR},
        'apply': {'type': 'boolean', 'default': False}
    },
    'required': ['action', 'session_path', 'expected_revision', 'manifest_paths'],
    'additionalProperties': False
}

VARIANT_CLEAN_SINGLE = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['clean']},
        'session_path': STR,
        'output_dir': STR,
        'apply': {'type': 'boolean'},
        'min_age_seconds': {'type': 'integer', 'minimum': 0},
        'preflight_only': {'type': 'boolean'},
    },
    'required': ['action', 'session_path', 'output_dir'],
    'additionalProperties': False,
}

VARIANT_CLEAN_BATCH = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['clean']},
        'session_paths': {
            'type': 'array',
            'items': STR,
            'minItems': 1,
            'uniqueItems': True,
        },
        'output_dir': STR,
        'apply': {'type': 'boolean'},
        'min_age_seconds': {'type': 'integer', 'minimum': 0},
        'preflight_only': {'type': 'boolean'},
    },
    'required': ['action', 'session_paths', 'output_dir'],
    'additionalProperties': False,
}

VARIANT_SESSION_INIT = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['session']},
        'operation': {'type': 'string', 'enum': ['init']},
        'session_path': STR,
        'source': STR,
        'base_profile': STR,
        'confirmed_brief': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'session_id': STR,
        'keep': {'type': 'array', 'items': {'type': 'string'}},
        'change': {'type': 'array', 'items': {'type': 'string'}},
        'exposure_ev': {'type': 'number', 'minimum': -2, 'maximum': 2},
        'adjustments': {'type': 'object'},
    },
    'required': ['action', 'operation', 'session_path', 'source', 'base_profile', 'confirmed_brief'],
    'additionalProperties': False,
}

VARIANT_SESSION_LOAD = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['session']},
        'operation': {'type': 'string', 'enum': ['load']},
        'session_path': STR,
    },
    'required': ['action', 'operation', 'session_path'],
    'additionalProperties': False,
}

VARIANT_SESSION_FEEDBACK = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string', 'enum': ['session']},
        'operation': {'type': 'string', 'enum': ['record_feedback']},
        'session_path': STR,
        'exposure_ev': {'type': 'number', 'minimum': -2, 'maximum': 2},
        'expected_revision': {'type': 'integer', 'minimum': 0},
        'user_feedback': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'confirmed_brief': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'keep': {'type': 'array', 'items': {'type': 'string'}},
        'change': {'type': 'array', 'items': {'type': 'string'}},
        'adjustments': {'type': 'object'},
        'reset_adjustments': {'type': 'boolean'},
        'reset_fields': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['action', 'operation', 'session_path', 'expected_revision'],
    'additionalProperties': False,
}

SCHEMA_VARIANTS = [
    VARIANT_DOCTOR,
    VARIANT_CAPABILITIES,
    VARIANT_INSPECT,
    VARIANT_PROFILE,
    VARIANT_QC,
    VARIANT_RAW_QC,
    VARIANT_RENDER,
    VARIANT_CLEAN_SINGLE,
    VARIANT_PRUNE_HISTORY,
    VARIANT_CLEAN_BATCH,
    VARIANT_SESSION_INIT,
    VARIANT_SESSION_LOAD,
    VARIANT_SESSION_FEEDBACK,
]

ACTIONS = {'doctor', 'capabilities', 'inspect', 'profile', 'qc', 'render', 'session', 'clean', 'raw_qc', 'prune_history'}


CLEAN_PREFLIGHT_CONFLICT = {
    'type': 'object', 'required': ['apply', 'preflight_only'],
    'properties': {'apply': {'enum': [True]}, 'preflight_only': {'enum': [True]}}
}

def schema():
    variants = copy.deepcopy(SCHEMA_VARIANTS)
    for variant in variants:
        if 'adjustments' in variant['properties']:
            variant['properties']['adjustments'] = {'$ref': '#/$defs/adjustments'}
        if variant['properties']['action']['enum'] == ['clean']:
            variant['not'] = CLEAN_PREFLIGHT_CONFLICT
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$defs': {'adjustments': adjustments_schema()},
        'oneOf': variants,
    }

class ToolError(Exception):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details
        super().__init__(message)

def require(condition, code, message):
    if not condition:
        raise ToolError(code, message)

HL_RECOVERY_METHODS = {
    'luminance': 'Luminance',
    'cielab': 'CIELab blending',
    'blend': 'Blend',
    'color': 'Color',
    'coloropp': 'Coloropp',
}

BAYER_DEMOSAIC_METHODS = {
    'ahd', 'amaze', 'amazebilinear', 'amazevng4', 'dcb', 'dcbbilinear', 'dcbvng4',
    'eahd', 'hphd', 'igv', 'lmmse', 'rcd', 'rcdbilinear', 'rcdvng4', 'vng4'
}

DUAL_DEMOSAIC_METHODS = {
    'amazebilinear', 'amazevng4', 'rcdbilinear', 'rcdvng4', 'dcbbilinear', 'dcbvng4'
}

LINE_NOISE_DIRECTIONS = {
    'horizontal': '1',
    'vertical': '2',
    'both': '3',
    1: '1',
    2: '2',
    3: '3',
    '1': '1',
    '2': '2',
    '3': '3',
}

RAW_ONLY_ADJUSTMENTS = {'highlight_recovery', 'demosaic', 'raw_preprocess', 'raw_calibration', 'lens_correction', 'input_profile'}
BAYER_EXCLUSIVE_CATEGORIES = {'demosaic', 'raw_calibration'}
BAYER_ONLY_RAW_PREPROCESS = {'line_noise', 'line_noise_direction', 'green_equilibration'}

ALLOWED_ADJUSTMENT_CATEGORIES = {
    'exposure_ev', 'contrast', 'saturation', 'white_balance', 'tone_curve', 'denoise', 'sharpen',
    'highlight_recovery', 'highlight_compression', 'demosaic', 'raw_preprocess', 'raw_calibration',
    'lens_correction', 'input_profile'
}

VARIANT_CAPABILITIES['properties']['parameters']['items']['enum'] = sorted(ALLOWED_ADJUSTMENT_CATEGORIES)

def check_adjustments_source_compatibility(adj, source_type, source_path=None):
    if not adj:
        return
    if source_type == 'jpeg':
        present_raw = set(adj) & RAW_ONLY_ADJUSTMENTS
        require(not present_raw, 'UNSUPPORTED_ADJUSTMENTS',
                f"Adjustment categories {sorted(present_raw)} are only supported for RAW sources, not JPEG")
    if source_path:
        p = Path(source_path)
        ext = p.suffix.lower()
        if ext != '.nef':
            present_bayer_cats = set(adj) & BAYER_EXCLUSIVE_CATEGORIES
            require(not present_bayer_cats, 'UNSUPPORTED_ADJUSTMENTS',
                    f"Bayer-exclusive adjustments {sorted(present_bayer_cats)} are only supported for Nikon .nef sources; got {ext}")
            if 'raw_preprocess' in adj:
                present_bayer_pre = set(adj['raw_preprocess']) & BAYER_ONLY_RAW_PREPROCESS
                require(not present_bayer_pre, 'UNSUPPORTED_ADJUSTMENTS',
                        f"Bayer preprocess fields {sorted(present_bayer_pre)} are only supported for Nikon .nef sources; got {ext}")

def validate_resource_mode(category, values, complete=False):
    mode = values.get('mode')
    if mode is None and not complete:
        return
    if category == 'lens_correction':
        fields_by_mode = {'none': set(), 'lensfun_auto': set(),
                          'lensfun_manual': {'camera_make', 'camera_model', 'lens'},
                          'lcp': {'path', 'sha256'}}
    else:
        fields_by_mode = {'camera_standard': set(), 'auto_matched': set(),
                          'custom': {'path', 'sha256'}}
    require(mode in fields_by_mode, 'INVALID_REQUEST', f'{category} requires a valid mode')
    specific = set().union(*fields_by_mode.values())
    permitted = fields_by_mode[mode]
    incompatible = (set(values) & specific) - permitted
    require(not incompatible, 'INVALID_REQUEST',
            f'{category}: mode {mode} cannot contain {sorted(incompatible)}; use reset_fields before switching modes')
    if complete:
        require(permitted <= set(values), 'INVALID_REQUEST',
                f'{category} mode {mode} requires {sorted(permitted)}')

def validate_adjustments_patch(adj, engine_ver):
    """Normalize and validate a partial patch; completeness belongs after inheritance."""
    validate_property(adj, adjustments_schema(), 'adjustments')
    if set(adj) - {'exposure_ev'}:
        require(engine_ver == '5.13', 'UNSUPPORTED_ADJUSTMENTS',
                f'New adjustments require verified RawTherapee 5.13 (detected engine: {engine_ver})')

    pts = adj.get('tone_curve', {}).get('points')
    if pts is not None:
        require(pts[0][0] == 0 and pts[-1][0] == 1, 'INVALID_REQUEST',
                'tone_curve.points x must start at 0 and end at 1')
        require(all(b[0] > a[0] and b[1] >= a[1] for a, b in zip(pts, pts[1:])),
                'INVALID_REQUEST', 'tone_curve.points x must strictly increase and y must not decrease')

    for category, extensions in [('lens_correction', {'.lcp'}),
                                 ('input_profile', {'.dcp', '.icc', '.icm'})]:
        values = adj.get(category, {})
        validate_resource_mode(category, values)
        for field in ('camera_make', 'camera_model', 'lens', 'path'):
            if field in values:
                require(not any(c in values[field] for c in ('\n', '\r', '\0')),
                        'INVALID_REQUEST', f'{category}.{field} must be single-line')
        if 'path' in values:
            p_val = values['path']
            if isinstance(p_val, str) and p_val.startswith('builtin:'):
                require(p_val in BUNDLED_PROFILES, 'INVALID_REQUEST', f'Unknown builtin profile: {p_val}')
                target_ext = BUNDLED_PROFILES[p_val].suffix.lower()
                require(target_ext in extensions, 'INVALID_REQUEST',
                        f'{category}.path builtin profile {p_val} must have extension in {sorted(extensions)}')
            else:
                require(Path(p_val).suffix.lower() in extensions, 'INVALID_REQUEST',
                        f'{category}.path must have extension in {sorted(extensions)}')

def merge_adjustments(base_adj, patch_adj):
    """Deep merges patch_adj into base_adj.
    Arrays (e.g. tone_curve points) are replaced as a whole.
    Dictionaries are recursively merged.
    Never mutates inputs.
    """
    def _deep_merge(base, patch):
        res = copy.deepcopy(base) if base else {}
        for k, v in patch.items():
            if isinstance(v, dict):
                if k not in res or not isinstance(res[k], dict):
                    res[k] = copy.deepcopy(v)
                else:
                    res[k] = _deep_merge(res[k], v)
            elif isinstance(v, list):
                res[k] = copy.deepcopy(v)
            else:
                res[k] = v
        return res

    return _deep_merge(base_adj or {}, patch_adj or {})

def validate_effective_adjustments(adj, engine_ver):
    """Full validation of effective adjustments after inheritance."""
    validate_adjustments_patch(adj, engine_ver)
    if 'white_balance' in adj:
        wb = adj['white_balance']
        require(set(wb) == {'temperature', 'green'}, 'INVALID_REQUEST',
                'white_balance requires both temperature and green to be specified without extra keys')
    if 'tone_curve' in adj:
        tc = adj['tone_curve']
        require(set(tc) == {'mode', 'points'}, 'INVALID_REQUEST',
                'tone_curve requires both mode and points without extra keys')
    if 'denoise' in adj:
        dn = adj['denoise']
        require(set(dn) & {'luma', 'chroma'} or dn.get('enabled') is False, 'INVALID_REQUEST',
                'denoise requires at least luma or chroma, or enabled: false')
    if 'sharpen' in adj:
        sh = adj['sharpen']
        require(set(sh) & {'amount', 'radius'} or sh.get('enabled') is False, 'INVALID_REQUEST',
                'sharpen requires at least amount or radius, or enabled: false')
    if 'highlight_recovery' in adj:
        hr = adj['highlight_recovery']
        if hr.get('enabled') is True:
            require('method' in hr, 'INVALID_REQUEST',
                    'highlight_recovery requires method when enabled is true')
    if 'demosaic' in adj:
        dm = adj['demosaic']
        require('method' in dm, 'INVALID_REQUEST', 'demosaic requires method')
        method = dm['method'].lower()
        if 'dual_contrast' in dm or 'dual_contrast_auto' in dm:
            require(method in DUAL_DEMOSAIC_METHODS, 'INVALID_REQUEST',
                    f"demosaic dual contrast controls require a dual demosaic method {sorted(DUAL_DEMOSAIC_METHODS)}, got '{dm['method']}'")
        if 'dual_contrast' in dm:
            require(dm.get('dual_contrast_auto') is False, 'INVALID_REQUEST',
                    'demosaic: dual_contrast threshold requires dual_contrast_auto to be explicitly false')
    for category, file_mode in [('lens_correction', 'lcp'), ('input_profile', 'custom')]:
        if category in adj:
            values = adj[category]
            validate_resource_mode(category, values, complete=True)
            if values['mode'] == file_mode:
                path = file_path(values['path'])
                actual_hash = digest(path)
                if category == 'lens_correction':
                    bundled_lcp_info(path, actual_hash)
                require(actual_hash == values['sha256'], 'CHECKSUM_MISMATCH',
                        f'{category} profile hash mismatch: expected {values["sha256"]}, got {actual_hash}')
                values['path'] = str(path)
    return adj

validate_adjustments = validate_effective_adjustments

def session_output_profile_paths(data):
    paths = []
    for h in data.get('history', []):
        export = h.get('export', {})
        require(isinstance(export, dict), 'CORRUPT_SESSION', 'Invalid history export metadata')
        profile = export.get('output_profile')
        if profile is not None:
            require(isinstance(profile, dict) and isinstance(profile.get('canonical_path'), str)
                    and Path(profile['canonical_path']).is_absolute(), 'CORRUPT_SESSION',
                    'Output ICC history resource must have an absolute canonical_path')
            paths.append(Path(profile['canonical_path']).resolve())
    return paths

def check_session_resource_anchors(session_data, sess_path):
    """Verify that all external resource paths in effective_adjustments and history
    are anchored absolute paths. Reject unanchored relative paths without guessing.
    """
    def _check_adj(adj, scope):
        if not isinstance(adj, dict):
            return
        lc = adj.get('lens_correction')
        if isinstance(lc, dict) and lc.get('mode') == 'lcp' and 'path' in lc:
            p_str = lc['path']
            if not Path(p_str).is_absolute() and p_str not in BUNDLED_PROFILES:
                raise ToolError('CORRUPT_SESSION',
                                f"Unanchored relative resource path in session {scope} (lens_correction.path='{p_str}'); re-bind with absolute path")
        ip = adj.get('input_profile')
        if isinstance(ip, dict) and ip.get('mode') == 'custom' and 'path' in ip:
            p_str = ip['path']
            if not Path(p_str).is_absolute():
                raise ToolError('CORRUPT_SESSION',
                                f"Unanchored relative resource path in session {scope} (input_profile.path='{p_str}'); re-bind with absolute path")

    session_output_profile_paths(session_data)
    _check_adj(session_data.get('effective_adjustments', {}), 'effective_adjustments')
    for idx, h in enumerate(session_data.get('history', [])):
        _check_adj(h.get('effective_adjustments', h.get('adjustments', {})), f'history[{idx}]')

def resolve_effective_adjustments(base_adj, req, engine_ver, source_type=None, source_path=None):
    """Shared inheritance -> reset -> patch -> validation pipeline
    for both render and session record_feedback.
    """
    has_reset_adj = 'reset_adjustments' in req
    has_reset_fields = 'reset_fields' in req

    if has_reset_adj and has_reset_fields:
        raise ToolError('INVALID_REQUEST', 'Cannot specify both reset_adjustments and reset_fields')

    if has_reset_fields:
        reset_fields = req.get('reset_fields')
        require(isinstance(reset_fields, list) and not isinstance(reset_fields, (str, bytes)),
                'INVALID_REQUEST', 'reset_fields must be an array')
        require(len(reset_fields) > 0, 'INVALID_REQUEST', 'reset_fields cannot be empty')
        require(len(reset_fields) == len(set(reset_fields)),
                'INVALID_REQUEST', 'reset_fields must not contain duplicate items')
        for f in reset_fields:
            require(isinstance(f, str) and bool(f.strip()),
                    'INVALID_REQUEST', 'reset_fields items must be non-empty strings')
            require('.' not in f,
                    'INVALID_REQUEST', f"reset_fields category cannot use dot-notation: '{f}'")
            require(f in ALLOWED_ADJUSTMENT_CATEGORIES,
                    'INVALID_REQUEST', f"Unknown reset_fields category '{f}'; allowed categories: {sorted(ALLOWED_ADJUSTMENT_CATEGORIES)}")

    # 1. 继承 (Inherit base)
    working_adj = copy.deepcopy(base_adj) if base_adj else {}

    # 2. Reset (Delete specified categories or all)
    if req.get('reset_adjustments') is True:
        working_adj = {}
    elif has_reset_fields and req.get('reset_fields') is not None:
        for f in req['reset_fields']:
            working_adj.pop(f, None)

    # 3. Patch
    req_adj = req.get('adjustments')
    if req_adj:
        validate_adjustments_patch(req_adj, engine_ver)
        if source_type:
            check_adjustments_source_compatibility(req_adj, source_type, source_path)

        # Anchor relative resource paths in incoming patch to absolute canonical paths against current cwd
        req_adj_anchored = copy.deepcopy(req_adj)
        dm_patch = req_adj_anchored.get('demosaic', {})
        method = dm_patch.get('method')
        if method in BAYER_DEMOSAIC_METHODS - DUAL_DEMOSAIC_METHODS:
            # Discard inherited dual-only state, never explicit conflicting user fields.
            inherited_dm = working_adj.get('demosaic', {})
            inherited_dm.pop('dual_contrast', None)
            inherited_dm.pop('dual_contrast_auto', None)
        if 'dual_contrast' in dm_patch and 'dual_contrast_auto' not in dm_patch:
            dm_patch['dual_contrast_auto'] = False
        if dm_patch.get('dual_contrast_auto') is True and 'dual_contrast' not in dm_patch:
            # An explicit return to auto discards only the inherited manual threshold.
            working_adj.get('demosaic', {}).pop('dual_contrast', None)
        if 'lens_correction' in req_adj_anchored and isinstance(req_adj_anchored['lens_correction'], dict):
            lc_patch = req_adj_anchored['lens_correction']
            inherited_mode = working_adj.get('lens_correction', {}).get('mode')
            if lc_patch.get('mode', inherited_mode) == 'lcp' and 'path' in lc_patch:
                p_val = lc_patch['path']
                if isinstance(p_val, str) and p_val in BUNDLED_PROFILES:
                    builtin, _ = profile(p_val, kind='lcp')
                    lc_patch['path'] = builtin['path']
                    if 'sha256' not in lc_patch:
                        lc_patch['sha256'] = builtin['sha256']
                else:
                    p = file_path(p_val)
                    lc_patch['path'] = str(p)

        working_adj = merge_adjustments(working_adj, req_adj_anchored)

    # Handle top-level exposure_ev
    if 'exposure_ev' in req:
        if req_adj and 'exposure_ev' in req_adj:
            raise ToolError('INVALID_REQUEST', 'Duplicate exposure_ev specified in both top-level and adjustments')
        working_adj['exposure_ev'] = req['exposure_ev']

    # 4. 完整校验 (Full validation)
    if working_adj:
        validate_effective_adjustments(working_adj, engine_ver)
        if source_type:
            check_adjustments_source_compatibility(working_adj, source_type, source_path)

    return working_adj

def find_variant_spec(req):
    require(isinstance(req, dict), 'INVALID_REQUEST', 'Request must be an object')
    action = req.get('action')
    require(isinstance(action, str) and action in ACTIONS, 'INVALID_REQUEST', f'Unknown action: {action}')

    if action == 'clean':
        has_sp = 'session_path' in req
        has_sps = 'session_paths' in req
        if (has_sp and has_sps) or (not has_sp and not has_sps):
            raise ToolError('INVALID_REQUEST', 'clean: exactly one of session_path or session_paths must be provided')
        return VARIANT_CLEAN_SINGLE if has_sp else VARIANT_CLEAN_BATCH

    if action == 'session':
        op = req.get('operation')
        require(isinstance(op, str) and not isinstance(op, bool), 'INVALID_REQUEST', 'session: missing or invalid operation')
        if op == 'init':
            return VARIANT_SESSION_INIT
        elif op == 'load':
            return VARIANT_SESSION_LOAD
        elif op == 'record_feedback':
            return VARIANT_SESSION_FEEDBACK
        else:
            raise ToolError('INVALID_REQUEST', f'session: unknown operation {op}')

    for v in SCHEMA_VARIANTS:
        if v['properties']['action']['enum'] == [action]:
            return v
    raise ToolError('INVALID_REQUEST', f'Unknown action: {action}')

def validate_property(value, rule, path_str, parent=None, parent_key=None):
    t = rule.get('type')
    if t == 'number':
        require(not isinstance(value, bool), 'INVALID_REQUEST', f'{path_str}: boolean not allowed for number')
        require(isinstance(value, (int, float)), 'INVALID_REQUEST', f'{path_str}: expected number')
        require(math.isfinite(value), 'INVALID_REQUEST', f'{path_str}: number must be finite (no NaN or Infinity)')
        if 'minimum' in rule:
            require(value >= rule['minimum'], 'INVALID_REQUEST', f'{path_str}: below minimum {rule["minimum"]}')
        if 'maximum' in rule:
            require(value <= rule['maximum'], 'INVALID_REQUEST', f'{path_str}: above maximum {rule["maximum"]}')
    elif t == 'integer':
        require(not isinstance(value, bool), 'INVALID_REQUEST', f'{path_str}: boolean not allowed for integer')
        if isinstance(value, float):
            require(math.isfinite(value) and value.is_integer(), 'INVALID_REQUEST',
                    f'{path_str}: expected integer, got non-integer float {value}')
            value = int(value)
            if parent is not None and parent_key is not None:
                parent[parent_key] = value
        require(type(value) is int, 'INVALID_REQUEST', f'{path_str}: expected integer')
        if 'minimum' in rule:
            require(value >= rule['minimum'], 'INVALID_REQUEST', f'{path_str}: below minimum {rule["minimum"]}')
        if 'maximum' in rule:
            require(value <= rule['maximum'], 'INVALID_REQUEST', f'{path_str}: above maximum {rule["maximum"]}')
    elif t == 'boolean':
        require(isinstance(value, bool), 'INVALID_REQUEST', f'{path_str}: expected boolean')
    elif t == 'string':
        require(isinstance(value, str) and not isinstance(value, bool), 'INVALID_REQUEST', f'{path_str}: expected string')
        require('\x00' not in value, 'INVALID_REQUEST', f'{path_str}: string cannot contain null bytes')
        if 'minLength' in rule:
            require(len(value) >= rule['minLength'], 'INVALID_REQUEST', f'{path_str}: length below minLength {rule["minLength"]}')
            if rule['minLength'] >= 1:
                require(bool(value.strip()), 'INVALID_REQUEST', f'{path_str}: string cannot be whitespace-only')
        if 'maxLength' in rule:
            require(len(value) <= rule['maxLength'], 'INVALID_REQUEST', f'{path_str}: exceeds maxLength {rule["maxLength"]}')
        if 'pattern' in rule:
            require(bool(re.fullmatch(rule['pattern'], value)), 'INVALID_REQUEST', f'{path_str}: invalid format')
    elif t == 'array':
        require(isinstance(value, list) and not isinstance(value, (str, bytes)), 'INVALID_REQUEST', f'{path_str}: expected array')
        if 'minItems' in rule:
            require(len(value) >= rule['minItems'], 'INVALID_REQUEST', f'{path_str}: array must have at least {rule["minItems"]} items')
        if 'maxItems' in rule:
            require(len(value) <= rule['maxItems'], 'INVALID_REQUEST', f'{path_str}: array must have at most {rule["maxItems"]} items')
        item_rule = rule.get('items')
        if item_rule:
            for idx, item in enumerate(value):
                validate_property(item, item_rule, f'{path_str}[{idx}]', parent=value, parent_key=idx)
        if rule.get('uniqueItems'):
            require(len(value) == len(set(value)), 'INVALID_REQUEST', f'{path_str}: items must be unique without duplicates')
    elif t == 'object':
        require(isinstance(value, dict), 'INVALID_REQUEST', f'{path_str}: expected object')
        if 'minProperties' in rule:
            require(len(value) >= rule['minProperties'], 'INVALID_REQUEST', f'{path_str}: object cannot be empty')
        if 'properties' in rule:
            props = rule['properties']
            for req_key in rule.get('required', []):
                require(req_key in value, 'INVALID_REQUEST', f'{path_str}: missing required field "{req_key}"')
            if rule.get('additionalProperties') is False:
                unknown = set(value) - set(props)
                require(not unknown, 'INVALID_REQUEST', f'{path_str}: unknown fields {sorted(unknown)}')
            for k, v in list(value.items()):
                if k in props:
                    validate_property(v, props[k], f'{path_str}.{k}', parent=value, parent_key=k)
    if 'enum' in rule:
        require(value in rule['enum'], 'INVALID_REQUEST', f'{path_str}: invalid enum {value}; must be one of {rule["enum"]}')
    if 'x-canonical-enum' in rule:
        value = next(v for v in rule['x-canonical-enum'] if v.lower() == value.lower())
        if parent is not None and parent_key is not None:
            parent[parent_key] = value
    if 'not' in rule:
        try:
            validate_property(copy.deepcopy(value), rule['not'], path_str)
        except ToolError:
            pass
        else:
            raise ToolError('INVALID_REQUEST', f'{path_str}: conflicting fields')

def validate(req):
    require(isinstance(req, dict), 'INVALID_REQUEST', 'Request must be an object')
    spec = find_variant_spec(req)
    for req_key in spec.get('required', []):
        require(req_key in req, 'INVALID_REQUEST', f'Missing required field: {req_key}')
    if spec.get('additionalProperties') is False:
        unknown = set(req) - set(spec['properties'])
        require(not unknown, 'INVALID_REQUEST', f'Unknown field(s): {sorted(unknown)}')
    for key, value in list(req.items()):
        rule = adjustments_schema() if key == 'adjustments' else spec['properties'][key]
        validate_property(value, rule, key, parent=req, parent_key=key)
    if req['action'] == 'clean':
        validate_property(req, {'not': CLEAN_PREFLIGHT_CONFLICT}, 'clean')
    return req

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def file_path(value):
    require(isinstance(value, (str, Path)) and not isinstance(value, bool), 'INVALID_REQUEST', f'Invalid path type: {type(value).__name__}')
    require(bool(str(value).strip()), 'INVALID_REQUEST', 'Path cannot be empty')
    require('\x00' not in str(value), 'INVALID_REQUEST', 'Path cannot contain null bytes')
    p = Path(value).expanduser().resolve()
    require(p.is_file(), 'FILE_NOT_FOUND', str(p))
    return p

def run(argv, timeout=30, settings=None, help_probe=False, stdout_only=False, threads=None):
    env = os.environ.copy()
    if settings:
        env['RT_SETTINGS'] = str(settings)
    if threads is not None:
        validate_property(threads, VARIANT_RENDER['properties']['threads'], 'threads')
        env['OMP_NUM_THREADS'] = str(int(threads))
        env['OMP_THREAD_LIMIT'] = str(int(threads))
    try:
        p = subprocess.run([str(v) for v in argv], shell=False, capture_output=True,
                           text=True, errors='replace', timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ToolError('TIMEOUT', 'Process exceeded timeout')
    require(p.returncode == 0 or (help_probe and p.returncode in (1, 255)), 'PROCESS_FAILED', (p.stderr + p.stdout)[-3000:])
    return p.stdout if stdout_only else p.stdout + p.stderr

def engine():
    candidates = [os.environ.get('RTCLI'), shutil.which('rawtherapee-cli'),
                  '/Applications/RawTherapee.app/Contents/MacOS/rawtherapee-cli-bin',
                  '/Applications/RawTherapee.app/Contents/MacOS/rawtherapee-cli']
    failures = []
    for candidate in dict.fromkeys(c for c in candidates if c):
        if not Path(candidate).is_file():
            continue
        try:
            with tempfile.TemporaryDirectory(prefix='rt-probe-') as settings:
                help_text = run([candidate, '-h'], settings=settings, help_probe=True)
            match = re.search(r'RawTherapee, version ([^,\s]+)', help_text)
            require(match and all(x in help_text for x in ['-js', '-q', '-p', '-c']),
                    'ENGINE_UNSUPPORTED', 'CLI did not return compatible help')
            return {'path': str(Path(candidate).resolve()), 'version': match.group(1)}
        except ToolError as e:
            failures.append(str(e))
    raise ToolError('ENGINE_UNAVAILABLE', '; '.join(failures) or 'RawTherapee CLI not found; set RTCLI')

def bundled_lcp_info(value, actual_hash=None):
    for alias, metadata in BUNDLED_LCP_METADATA.items():
        if str(value) == alias or Path(value).expanduser().resolve() == BUNDLED_PROFILES[alias].resolve():
            if actual_hash is not None:
                require(actual_hash == metadata['expected_sha256'], 'CHECKSUM_MISMATCH',
                        f'Bundled LCP bytes changed: {alias}')
            return {'alias': alias, **metadata}
    return None

def parse_icc(payload):
    require(importlib.util.find_spec('PIL') is not None, 'DEPENDENCY_MISSING',
            'Pillow with ImageCms is required to parse ICC profiles')
    from PIL import ImageCms
    try:
        icc = ImageCms.ImageCmsProfile(io.BytesIO(payload))
        return icc, {'description': ImageCms.getProfileDescription(icc).strip(),
                     'color_space': icc.profile.xcolor_space.strip(),
                     'device_class': icc.profile.device_class.strip()}
    except Exception as exc:
        raise ToolError('PROFILE_INVALID', f'Cannot parse ICC profile: {exc}') from exc

def profile(value, kind='pp3'):
    if isinstance(value, str) and value.startswith('builtin:'):
        require(value in BUNDLED_PROFILES, 'INVALID_REQUEST',
                f'Unknown or incompatible builtin profile: {value}')
        target_path = BUNDLED_PROFILES[value]
        ext = target_path.suffix.lower()
        if kind == 'pp3':
            require(ext == '.pp3', 'INVALID_REQUEST',
                    f'Builtin profile {value} is not a PP3 profile')
        elif kind == 'lcp':
            require(ext == '.lcp', 'INVALID_REQUEST',
                    f'Builtin profile {value} is not an LCP profile')
        else:
            require(False, 'INVALID_REQUEST', f'Unsupported kind for builtin profile: {kind}')
        value = target_path
    p = file_path(value)
    require(p.is_file(), 'FILE_NOT_FOUND', f'Profile file not found: {p}')
    require(kind != 'pp3' or p.suffix.lower() not in {'.icc', '.icm'}, 'INVALID_REQUEST',
            'ICC files require profile kind=icc; kind defaults to pp3')
    icc_payload = p.read_bytes() if kind == 'icc' else None
    p_sha = hashlib.sha256(icc_payload).hexdigest() if icc_payload is not None else digest(p)
    if kind != 'pp3':
        ext = p.suffix.lower()
        if kind == 'dcp':
            require(ext == '.dcp', 'INVALID_REQUEST', f'DCP profile requires .dcp extension, got {ext}')
        elif kind == 'icc':
            require(ext in ('.icc', '.icm'), 'INVALID_REQUEST', f'ICC profile requires .icc or .icm extension, got {ext}')
        elif kind == 'lcp':
            require(ext == '.lcp', 'INVALID_REQUEST', f'LCP profile requires .lcp extension, got {ext}')
        else:
            raise ToolError('INVALID_REQUEST', f'Unsupported profile kind: {kind}')
        result = {
            'path': str(p.resolve()),
            'sha256': p_sha,
            'kind': kind,
            'size_bytes': p.stat().st_size,
            'verification_status': 'file_integrity_verified',
            'effect_verified': False,
            'compatibility_note': 'File integrity and SHA-256 verified. Camera/lens optical model matching or color rendering accuracy cannot be guaranteed without scene calibration evidence.'
        }
        if kind == 'icc':
            _, result['icc'] = parse_icc(icc_payload)
        if kind == 'lcp':
            info = bundled_lcp_info(p, p_sha)
            if info:
                result['builtin_profile'] = info
                result['warnings'] = render_warnings({'lens_correction': {'mode': 'lcp', 'path': str(p)}})
        return result, None

    text = p.read_text(encoding='utf-8-sig')
    cfg = configparser.ConfigParser(interpolation=None, strict=True)
    cfg.optionxform = str
    cfg.read_string(text)
    def field(section, key):
        return cfg.get(section, key, fallback='').strip()
    working, output = field('Color Management', 'WorkingProfile'), field('Color Management', 'OutputProfile')
    require(working and output and output.lower() not in ['(none)', 'none', '(no icm)'],
            'PROFILE_NOT_PINNED', 'Save explicit WorkingProfile and OutputProfile in the GUI PP3')
    return {'path': str(p), 'sha256': p_sha, 'kind': 'pp3', 'working_profile': working,
            'output_profile': output, 'app_version': field('Version', 'AppVersion'),
            'exposure_patchable': cfg.has_option('Exposure', 'Compensation') and field('Exposure', 'Auto').lower() == 'false'}, text

def apply_pp3_patches(text, patch_dict):
    """
    Applies section-scoped key-value updates to PP3 text while strictly preserving
    all existing byte content, ordering, and comments. Missing keys are added to existing
    sections; missing sections are cleanly appended.
    """
    lines = text.splitlines(keepends=True)
    new_lines = []
    current_section = None
    remaining_patches = {sec: dict(keys) for sec, keys in patch_dict.items()}

    for line in lines:
        m = re.fullmatch(r'\s*\[([^\]]+)\]\s*', line.strip())
        if m:
            if current_section and current_section in remaining_patches:
                for k, v in remaining_patches[current_section].items():
                    new_lines.append(f'{k}={v}\n')
                del remaining_patches[current_section]
            current_section = m.group(1)
            new_lines.append(line)
            continue

        if current_section and current_section in remaining_patches:
            km = re.match(r'^\s*([^=]+?)\s*=', line)
            if km:
                key = km.group(1).strip()
                if key in remaining_patches[current_section]:
                    val = remaining_patches[current_section].pop(key)
                    new_lines.append(f'{key}={val}\n')
                    continue
        new_lines.append(line)

    if current_section and current_section in remaining_patches:
        for k, v in remaining_patches[current_section].items():
            new_lines.append(f'{k}={v}\n')
        del remaining_patches[current_section]

    for sec, keys in remaining_patches.items():
        if not new_lines or not new_lines[-1].endswith('\n\n'):
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines.append('\n')
            new_lines.append('\n')
        new_lines.append(f'[{sec}]\n')
        for k, v in keys.items():
            new_lines.append(f'{k}={v}\n')

    return ''.join(new_lines)

def patch_exposure(text, ev):
    section = None
    lines = text.splitlines(keepends=True)
    hits = []
    for i, line in enumerate(lines):
        m = re.fullmatch(r'\s*\[([^\]]+)\]\s*', line.strip())
        if m:
            section = m.group(1)
        elif section == 'Exposure' and re.match(r'^\s*Compensation\s*=', line):
            hits.append(i)
    require(len(hits) == 1, 'PROFILE_INVALID', 'Expected exactly one Exposure.Compensation')
    i = hits[0]
    lines[i] = f'Compensation={ev:.8g}\n'
    return ''.join(lines)

def preview_exif(source_exif, policy, size):
    from PIL import Image, ExifTags
    result = Image.Exif()
    if policy == 'strip':
        return b'', []
    nested = source_exif.get_ifd(0x8769) if source_exif else {}
    capture = [36867, 36868, 36881, 36882, 37521, 37522]
    camera = [33434, 33437, 34855, 37386, 42035, 42036] if policy == 'capture_camera' else []
    fields, retained = {}, []
    for key in capture + camera:
        value = nested.get(key, source_exif.get(key) if source_exif else None)
        if value is not None:
            fields[key] = value
            retained.append(ExifTags.TAGS.get(key, str(key)))
    if policy == 'capture_camera' and source_exif:
        for key in [271, 272]:
            if source_exif.get(key) is not None:
                result[key] = source_exif[key]
                retained.append(ExifTags.TAGS[key])
    result[274] = 1
    fields.update({40962: size[0], 40963: size[1], 40961: 1})
    result[0x8769] = fields
    return result.tobytes(), retained

def write_preview(rgb, icc, target, metadata_policy='strip', source_exif=None):
    """Viewing proxy from the rendered raster; never rewrite the full output."""
    from PIL import Image, ImageCms, ImageOps
    require(icc, 'OUTPUT_INVALID', 'Output missing embedded ICC')
    scale = min(1, 1600 / max(rgb.size))
    size = tuple(max(1, round(v * scale)) for v in rgb.size)
    small = ImageOps.exif_transpose(rgb.resize(size, Image.Resampling.LANCZOS))
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
    try:
        small = ImageCms.profileToProfile(small, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                         srgb, outputMode='RGB')
        exif_bytes, retained = preview_exif(source_exif, metadata_policy, small.size)
        small.save(target, 'JPEG', quality=85, subsampling=0, optimize=True,
                   progressive=True, icc_profile=srgb.tobytes(), exif=exif_bytes)
    except Exception as exc:
        raise ToolError('PREVIEW_FAILED', f'Cannot create color-managed viewing proxy: {exc}')
    return {'path': str(target), 'sha256': digest(target), 'width': small.width,
            'height': small.height, 'size_bytes': Path(target).stat().st_size,
            'color_space': 'sRGB', 'source': 'rendered_output',
            'metadata_policy': metadata_policy, 'retained_exif_tags': retained,
            'scope': '8-bit viewing proxy; inspect the full output for 100% detail and precision.'}

def qc(value, preview_path=None, preview_metadata='strip'):
    from PIL import Image, ImageCms, ImageStat
    p = file_path(value)
    with Image.open(p) as im:
        im.load()
        icc = im.info.get('icc_profile')
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() if icc else None
        size = list(im.size)
        bits = im.tag_v2.get(258) if im.format == 'TIFF' else None
        if isinstance(bits, int):
            bits = [bits]
        elif bits is not None:
            bits = list(bits)
        rgb = im.convert('RGB')
        preview = write_preview(rgb, icc, preview_path, preview_metadata, im.getexif()) if preview_path is not None else None
        rgb.thumbnail((1024, 1024))
        hist = rgb.convert('L').histogram()
        n = sum(hist)
        mean = ImageStat.Stat(rgb).mean
        result = {'width': size[0], 'height': size[1], 'icc_description': desc,
                'icc_sha256': hashlib.sha256(icc).hexdigest() if icc else None,
                'mean_rgb': [v / 255 for v in mean],
                'mean_luma': sum(i * count for i, count in enumerate(hist)) / (255 * n),
                'shadow_clip_pct': 100 * sum(hist[:3]) / n,
                'highlight_clip_pct': 100 * sum(hist[253:]) / n,
                'measurement': '8-bit encoded RGB/L after conversion, <=1024px thumbnail, thresholds <=2 and >=253',
                'file_bits_per_sample': bits,
                'precision_verified': False,
                'qc_scope': 'Preview tonal-outlier screening only; not RAW clipping, linear-light analysis, or effective 16-bit precision verification',
                'visual_review_required': True}
        if preview is not None:
            result['preview'] = preview
        return result

def raw_qc(req):
    p = file_path(req['source'])
    src_info = check_source(p)
    require(src_info['source_type'] == 'raw' and p.suffix.lower() == '.nef',
            'UNSUPPORTED_INPUT',
            f"raw_qc currently only supports Nikon .nef Bayer RAW files; got '{p.suffix}'")

    try:
        import rawpy
        import numpy as np
    except ImportError as e:
        raise ToolError('DEPENDENCY_MISSING',
                        f"raw_qc requires 'rawpy' and 'numpy' for Bayer CFA diagnostic inspection. "
                        f"Install in your current Python environment (e.g., uv run --with rawpy --with numpy ... or pip install rawpy numpy). Details: {e}")
    except Exception as e:
        raise ToolError('DEPENDENCY_MISSING',
                        f"Failed to initialize LibRaw/rawpy runtime for raw_qc: {e}")

    try:
        raw_ctx = rawpy.imread(str(p))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError('CORRUPT_INPUT', f"Failed to open RAW file with LibRaw: {e}")

    with raw_ctx as raw:
        try:
            raw_image = raw.raw_image_visible
            raw_colors = raw.raw_colors_visible
        except Exception as e:
            raise ToolError('PROCESS_FAILED', f"Failed to extract visible CFA data: {e}")

        require(raw_image is not None and getattr(raw_image, 'ndim', None) == 2,
                'UNSUPPORTED_INPUT', 'raw_qc requires a 2D raw_image_visible sensor array')
        require(raw_colors is not None and getattr(raw_colors, 'ndim', None) == 2,
                'UNSUPPORTED_INPUT', 'raw_qc requires a 2D raw_colors_visible CFA layout')
        require(raw_image.shape == raw_colors.shape,
                'CORRUPT_INPUT',
                f"raw_image shape {raw_image.shape} does not match raw_colors shape {raw_colors.shape}")

        h_vis, w_vis = raw_image.shape
        require(h_vis > 0 and w_vis > 0, 'CORRUPT_INPUT', 'Visible sensor area is empty')

        c_desc_raw = getattr(raw, 'color_desc', b'RGBG')
        if isinstance(c_desc_raw, (bytes, bytearray)):
            c_desc_str = c_desc_raw.decode('latin1', errors='replace')
        else:
            c_desc_str = str(c_desc_raw)

        unique_colors_in_sensor = sorted(int(c) for c in np.unique(raw_colors))
        require(len(unique_colors_in_sensor) in (3, 4) and max(unique_colors_in_sensor) < max(4, len(c_desc_str)),
                'UNSUPPORTED_INPUT',
                f"Non-Bayer CFA layout detected ({len(unique_colors_in_sensor)} channels in raw_colors); raw_qc only supports 2D Bayer CFA")

        roi_req = req.get('roi')
        if roi_req is not None:
            require(isinstance(roi_req, dict) and bool(roi_req), 'INVALID_REQUEST', 'roi must be an object')
            require(set(roi_req.keys()) <= {'x', 'y', 'width', 'height'} and {'x', 'y', 'width', 'height'} <= set(roi_req.keys()),
                    'INVALID_REQUEST', f'roi must contain exactly x, y, width, height (got {sorted(roi_req.keys())})')
            rx = roi_req['x']
            ry = roi_req['y']
            rw = roi_req['width']
            rh = roi_req['height']
            require(type(rx) is int and not isinstance(rx, bool) and rx >= 0,
                    'INVALID_REQUEST', f'roi.x: must be non-negative integer, got {rx!r}')
            require(type(ry) is int and not isinstance(ry, bool) and ry >= 0,
                    'INVALID_REQUEST', f'roi.y: must be non-negative integer, got {ry!r}')
            require(type(rw) is int and not isinstance(rw, bool) and rw >= 1,
                    'INVALID_REQUEST', f'roi.width: must be positive integer >= 1, got {rw!r}')
            require(type(rh) is int and not isinstance(rh, bool) and rh >= 1,
                    'INVALID_REQUEST', f'roi.height: must be positive integer >= 1, got {rh!r}')
            require(rx + rw <= w_vis and ry + rh <= h_vis,
                    'INVALID_REQUEST',
                    f'ROI [x={rx}, y={ry}, w={rw}, h={rh}] exceeds visible CFA dimensions [w={w_vis}, h={h_vis}]; no silent clamping')
            sub_img = raw_image[ry:ry+rh, rx:rx+rw]
            sub_colors = raw_colors[ry:ry+rh, rx:rx+rw]
            roi_info = {
                'x': rx,
                'y': ry,
                'width': rw,
                'height': rh,
                'applied': True,
                'coordinates_scope': 'unrotated_visible_cfa'
            }
        else:
            sub_img = raw_image
            sub_colors = raw_colors
            roi_info = {
                'x': 0,
                'y': 0,
                'width': w_vis,
                'height': h_vis,
                'applied': False,
                'coordinates_scope': 'unrotated_visible_cfa'
            }

        channels = []
        channel_indices = range(len(c_desc_str)) if len(c_desc_str) in (3, 4) else unique_colors_in_sensor

        per_ch_black = getattr(raw, 'black_level_per_channel', None)
        global_black = getattr(raw, 'black_level', None)
        per_ch_white = getattr(raw, 'camera_white_level_per_channel', None)
        global_white = getattr(raw, 'white_level', None)

        for c_idx in channel_indices:
            color_label = c_desc_str[c_idx] if c_idx < len(c_desc_str) else f'Ch{c_idx}'
            mask = (sub_colors == c_idx)
            ch_pixels = sub_img[mask]
            count = int(ch_pixels.size)

            black_level = None
            black_source = 'no_credible_black_level'
            if per_ch_black is not None and c_idx < len(per_ch_black) and per_ch_black[c_idx] is not None:
                bv = per_ch_black[c_idx]
                if isinstance(bv, (int, float)) and not isinstance(bv, bool) and math.isfinite(bv):
                    black_level = float(bv)
                    black_source = 'black_level_per_channel'
                else:
                    black_source = 'invalid_black_level_per_channel'
            elif global_black is not None:
                if isinstance(global_black, (int, float)) and not isinstance(global_black, bool) and math.isfinite(global_black):
                    black_level = float(global_black)
                    black_source = 'decoder_black_level_global'
                else:
                    black_source = 'invalid_global_black_level'

            white_level = None
            white_source = 'no_credible_white_level'
            cand_white = None
            if per_ch_white is not None and c_idx < len(per_ch_white) and per_ch_white[c_idx] is not None:
                wv = per_ch_white[c_idx]
                if isinstance(wv, (int, float)) and not isinstance(wv, bool) and math.isfinite(wv):
                    cand_white = float(wv)
                    white_source = 'camera_white_level_per_channel'
                else:
                    white_source = 'invalid_camera_white_level_per_channel'
            if cand_white is None:
                if global_white is not None and isinstance(global_white, (int, float)) and not isinstance(global_white, bool) and math.isfinite(global_white):
                    cand_white = float(global_white)
                    white_source = 'decoder_white_level_fallback'

            if cand_white is not None and black_level is not None:
                if cand_white <= black_level:
                    cand_white = None
                    white_source = 'invalid_levels_white_le_black'

            if cand_white is not None:
                white_level = float(cand_white)

            if count > 0:
                min_val = int(ch_pixels.min())
                max_val = int(ch_pixels.max())
                mean_val = float(round(float(ch_pixels.mean()), 2))
            else:
                min_val = None
                max_val = None
                mean_val = None

            clip_white_pct = None
            if count > 0 and white_level is not None:
                n_clip = int(np.sum(ch_pixels >= white_level))
                clip_white_pct = float(round(100.0 * n_clip / count, 4))

            sub_black_pct = None
            if count > 0 and black_level is not None:
                n_black = int(np.sum(ch_pixels <= black_level))
                sub_black_pct = float(round(100.0 * n_black / count, 4))

            channels.append({
                'channel_index': int(c_idx),
                'color_desc': color_label,
                'sample_count': count,
                'min': min_val,
                'max': max_val,
                'mean': mean_val,
                'black_level': black_level,
                'white_level': white_level,
                'threshold_sources': {
                    'black': black_source,
                    'white': white_source
                },
                'clip_white_pct': clip_white_pct,
                'sub_black_pct': sub_black_pct
            })

        pattern_raw = getattr(raw, 'raw_pattern', None)
        cfa_pattern = pattern_raw.tolist() if hasattr(pattern_raw, 'tolist') else None

        rawpy_ver = getattr(rawpy, '__version__', 'unknown')
        libraw_ver = getattr(rawpy, 'libraw_version', 'unknown')

        return {
            'source': str(p),
            'source_sha256': digest(p),
            'source_type': 'raw',
            'container_check': src_info.get('container_check', 'tiff_header_valid'),
            'decoder': {
                'name': 'rawpy',
                'rawpy_version': rawpy_ver,
                'libraw_version': libraw_ver
            },
            'raw_dimensions': {
                'visible_width': w_vis,
                'visible_height': h_vis,
                'cfa_pattern': cfa_pattern,
                'color_desc': c_desc_str,
                'coordinates_note': 'Unrotated visible CFA sensor coordinates'
            },
            'roi': roi_info,
            'channels': channels,
            'scope_and_disclaimers': {
                'measurement': 'Diagnostic metrics computed directly on unrotated visible CFA raw sensor code values via rawpy/LibRaw',
                'sensor_accuracy': 'Raw code values reflect decoder-visible CFA counts and do not represent original ADC physical ground truth',
                'recoverability': 'Saturation counts indicate codes reaching or exceeding decoder white level; does not prove highlight recoverability, 16-bit linear precision, or aesthetic balance',
                'raw_calibration_mapping': 'Does not automatically map to raw_calibration parameters (RT black offsets or white scale)',
                'visual_review_required': True
            }
        }

def check_render_space(target):
    probe = Path(target)
    while not probe.exists():
        probe = probe.parent
    require(probe.is_dir(), 'INVALID_REQUEST', f'Output ancestor is not a directory: {probe}')
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        raise ToolError('DISK_CHECK_FAILED', f'Cannot check output filesystem {probe}: {exc}') from exc
    require(free >= MIN_RENDER_FREE_BYTES, 'INSUFFICIENT_DISK_SPACE',
            f'Output filesystem {probe} has {free} free bytes; at least {MIN_RENDER_FREE_BYTES} bytes (500 MiB) required. No render started.')
    return {'checked_path': str(probe), 'free_bytes': free, 'required_free_bytes': MIN_RENDER_FREE_BYTES}

def output_profile_info(spec):
    from PIL import ImageCms
    path = file_path(spec['path'])
    require(path.suffix.lower() in {'.icc', '.icm'} and not any(c in str(path) for c in ('\n', '\r')),
            'INVALID_REQUEST', 'Output profile must be a single-line ICC/ICM path')
    payload = path.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    require(actual_hash == spec['sha256'], 'CHECKSUM_MISMATCH', 'Output ICC profile hash mismatch')
    try:
        profile, metadata = parse_icc(payload)
        require(metadata['color_space'] == 'RGB', 'PROFILE_INVALID', 'Output ICC must describe RGB data')
        ImageCms.buildTransformFromOpenProfiles(ImageCms.createProfile('sRGB'), profile, 'RGB', 'RGB')
        desc = metadata['description']
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError('PROFILE_INVALID', f'Output ICC is not a usable RGB output profile: {exc}') from exc
    return {'canonical_path': str(path), 'sha256': actual_hash, 'description': desc}

def local_output_profiles(engine_path):
    executable = Path(engine_path).resolve()
    bundle = next((p for p in executable.parents if p.name == 'Contents' and p.parent.suffix == '.app'), None)
    root = (bundle / 'Resources/share/iccprofiles/output') if bundle else (executable.parent.parent / 'share/rawtherapee/iccprofiles/output')
    if not root.is_dir():
        return []
    profiles = []
    for path in sorted(root.iterdir(), key=lambda p: (p.name.casefold(), p.name)):
        if path.suffix.lower() not in {'.icc', '.icm'} or not path.is_file():
            continue
        entry = {'name': path.stem, 'path': str(path)}
        try:
            entry['sha256'] = digest(path)
        except OSError as exc:
            entry.update(sha256=None, error={'code': 'PROFILE_UNREADABLE', 'message': str(exc)})
        profiles.append(entry)
    return profiles

def render(req):
    source = file_path(req['source'])
    src_info = check_source(source)
    require(importlib.util.find_spec('PIL') is not None, 'DEPENDENCY_MISSING', 'Pillow is required for output validation')
    rt = engine()
    require(rt['version'] == req['engine_version'], 'VERSION_MISMATCH', 'Engine version changed; review profile first')
    meta, text = profile(req['profile'])
    require(meta['sha256'] == req['profile_sha256'], 'PROFILE_CHANGED', 'Profile hash mismatch')
    require(not meta['app_version'] or meta['app_version'] == rt['version'],
            'VERSION_MISMATCH', 'GUI profile version differs; re-save and canary-test in current engine')

    # Ambiguity check: duplicate exposure_ev in both top-level and adjustments
    has_top_ev = 'exposure_ev' in req
    has_adj = 'adjustments' in req
    req_adj = req.get('adjustments') or {}
    if has_top_ev and has_adj and 'exposure_ev' in req_adj:
        raise ToolError('INVALID_REQUEST', 'Duplicate exposure_ev specified in both top-level and adjustments')

    if has_adj:
        validate_adjustments_patch(req_adj, rt['version'])
        check_adjustments_source_compatibility(req_adj, src_info['source_type'], str(source))

    output_icc = output_profile_info(req['output_profile']) if 'output_profile' in req else None

    # Heavy I/O outside lock
    src_hash = digest(source)
    prof_path = Path(meta['path'])

    # Session binding verification
    has_session = 'session_path' in req or 'expected_revision' in req
    render_gate = None
    if has_session:
        require('session_path' in req and 'expected_revision' in req, 'INVALID_REQUEST',
                'session_path and expected_revision must be provided together')
        exp_rev = req['expected_revision']
        require(type(exp_rev) is int and not isinstance(exp_rev, bool) and exp_rev >= 0,
                'INVALID_REQUEST', 'expected_revision must be non-negative integer')
        sess_path = Path(req['session_path']).expanduser().resolve()
        require(sess_path.is_file(), 'FILE_NOT_FOUND', f'Session file not found: {sess_path}')
        sess_lock_path = Path(str(sess_path) + '.lock')
        sess_gate_path = Path(str(sess_path) + '.render.gate')

        render_gate = FileLock(sess_gate_path)
        if not render_gate.acquire(non_blocking=True):
            raise ToolError('RENDER_IN_PROGRESS', f'A render is already in progress for session: {sess_path}')
    else:
        sess_path = None
        sess_lock_path = None
        sess_gate_path = None
        sess_id = None
        exp_rev = None
        base_adj = {}
        intent_snapshot = None

    folder = None
    active_lock = None
    manifest_complete = False
    try:
        if has_session:
            # Short Lock 1: read session snapshot
            with FileLock(sess_lock_path):
                try:
                    raw = sess_path.read_text(encoding='utf-8')
                    session_data = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ToolError('CORRUPT_SESSION', f'Session file contains invalid JSON: {e}')
                except Exception as e:
                    raise ToolError('CORRUPT_SESSION', f'Cannot read session file: {e}')

                require(isinstance(session_data, dict), 'CORRUPT_SESSION', 'Session file content must be a JSON object')
                ver = session_data.get('schema_version')
                require(ver == 1, 'UNSUPPORTED_SESSION_SCHEMA',
                        f'Unsupported session schema_version: {ver}; expected 1. Do not auto-reset.')
                check_session_resource_anchors(session_data, sess_path)

                cur_rev = session_data.get('revision', 0)
                require(cur_rev == exp_rev, 'REVISION_CONFLICT',
                        f'Session revision conflict: expected {exp_rev}, but file is at {cur_rev}. Reload before updating.')

                sess_id = session_data.get('session_id')
                require(bool(sess_id), 'CORRUPT_SESSION', 'Session missing session_id')

                sess_src = Path(session_data['source']['path']).resolve()
                sess_src_hash = session_data['source']['sha256']
                sess_prof = Path(session_data['base_profile']['path']).resolve()
                sess_prof_hash = session_data['base_profile']['sha256']

                base_adj = copy.deepcopy(session_data.get('effective_adjustments', {}))

                latest_feedback = None
                for h in reversed(session_data.get('history', [])):
                    if h.get('user_feedback'):
                        latest_feedback = h['user_feedback']
                        break
                intent_snapshot = {
                    'confirmed_brief': session_data.get('confirmed_brief'),
                    'keep': list(session_data.get('keep', [])),
                    'change': list(session_data.get('change', [])),
                    'user_feedback': latest_feedback
                }

        # Outside lock verification
        if has_session:
            require(source == sess_src, 'SESSION_SOURCE_MISMATCH',
                    f'Render source {source} does not match session source {sess_src}')
            require(src_hash == sess_src_hash, 'SESSION_SOURCE_CHANGED',
                    'Source file hash does not match session source hash')
            require(prof_path == sess_prof, 'SESSION_PROFILE_MISMATCH',
                    f'Render profile {prof_path} does not match session base_profile {sess_prof}')
            require(meta['sha256'] == sess_prof_hash, 'SESSION_PROFILE_CHANGED',
                    'Base profile hash does not match session base_profile hash')

        # Resolve effective adjustments via shared pipeline
        effective_adj = resolve_effective_adjustments(
            base_adj, req, rt['version'],
            source_type=src_info['source_type'],
            source_path=str(source)
        )

        # Build PP3 patches from effective adjustments
        patch_dict = {}
        ev = effective_adj.get('exposure_ev')
        if ev is not None:
            require(meta['exposure_patchable'], 'PROFILE_INVALID', 'Exposure patch requires existing Compensation and Auto=false')
            patch_dict.setdefault('Exposure', {})['Compensation'] = f'{ev:.8g}'

        if 'contrast' in effective_adj:
            patch_dict.setdefault('Exposure', {})['Contrast'] = str(effective_adj['contrast'])
        if 'saturation' in effective_adj:
            patch_dict.setdefault('Exposure', {})['Saturation'] = str(effective_adj['saturation'])
        if 'white_balance' in effective_adj:
            wb = effective_adj['white_balance']
            wb_sec = patch_dict.setdefault('White Balance', {})
            wb_sec['Enabled'] = 'true'
            wb_sec['Setting'] = 'Custom'
            wb_sec['Temperature'] = str(wb['temperature'])
            wb_sec['Green'] = f"{wb['green']:.8g}"
        if 'tone_curve' in effective_adj:
            tc = effective_adj['tone_curve']
            exp_sec = patch_dict.setdefault('Exposure', {})
            exp_sec['CurveMode'] = tc['mode']
            exp_sec['Curve'] = '1;' + ';'.join(f'{pt[0]:.8g};{pt[1]:.8g}' for pt in tc['points']) + ';'
        if 'denoise' in effective_adj:
            dn = effective_adj['denoise']
            dn_sec = patch_dict.setdefault('Directional Pyramid Denoising', {})
            dn_sec['Enabled'] = 'true' if dn.get('enabled', True) else 'false'
            dn_sec['CMethod'] = 'MAN'
            dn_sec['C2Method'] = 'MANU'
            if 'luma' in dn:
                dn_sec['Luma'] = f"{dn['luma']:.8g}"
            if 'chroma' in dn:
                dn_sec['Chroma'] = f"{dn['chroma']:.8g}"
        if 'sharpen' in effective_adj:
            sh = effective_adj['sharpen']
            sh_sec = patch_dict.setdefault('Sharpening', {})
            sh_sec['Enabled'] = 'true' if sh.get('enabled', True) else 'false'
            sh_sec['Method'] = 'usm'
            if 'amount' in sh:
                sh_sec['Amount'] = str(sh['amount'])
            if 'radius' in sh:
                sh_sec['Radius'] = f"{sh['radius']:.8g}"

        if 'highlight_recovery' in effective_adj:
            hr = effective_adj['highlight_recovery']
            hr_sec = patch_dict.setdefault('HLRecovery', {})
            if 'enabled' in hr:
                hr_sec['Enabled'] = 'true' if hr['enabled'] else 'false'
            if 'method' in hr:
                m_str = hr['method']
                hr_sec['Method'] = HL_RECOVERY_METHODS.get(m_str.lower(), m_str)

        if 'highlight_compression' in effective_adj:
            hc = effective_adj['highlight_compression']
            exp_sec = patch_dict.setdefault('Exposure', {})
            if 'amount' in hc:
                exp_sec['HighlightCompr'] = str(hc['amount'])
            if 'threshold' in hc:
                exp_sec['HighlightComprThreshold'] = str(hc['threshold'])

        if 'demosaic' in effective_adj:
            dm = effective_adj['demosaic']
            bayer_sec = patch_dict.setdefault('RAW Bayer', {})
            if 'method' in dm:
                bayer_sec['Method'] = dm['method'].lower()
            if 'dual_contrast_auto' in dm:
                bayer_sec['DualDemosaicAutoContrast'] = 'true' if dm['dual_contrast_auto'] else 'false'
            if 'dual_contrast' in dm:
                c = dm['dual_contrast']
                bayer_sec['DualDemosaicContrast'] = str(int(c)) if isinstance(c, int) or (isinstance(c, float) and c.is_integer()) else f"{c:.8g}"

        if 'raw_preprocess' in effective_adj:
            rp = effective_adj['raw_preprocess']
            if 'hot_pixel_filter' in rp:
                patch_dict.setdefault('RAW', {})['HotPixelFilter'] = 'true' if rp['hot_pixel_filter'] else 'false'
            if 'dead_pixel_filter' in rp:
                patch_dict.setdefault('RAW', {})['DeadPixelFilter'] = 'true' if rp['dead_pixel_filter'] else 'false'
            if 'hot_dead_pixel_threshold' in rp:
                patch_dict.setdefault('RAW', {})['HotDeadPixelThresh'] = str(rp['hot_dead_pixel_threshold'])

            if 'line_noise' in rp:
                patch_dict.setdefault('RAW Bayer', {})['LineDenoise'] = str(rp['line_noise'])
            if 'line_noise_direction' in rp:
                d = rp['line_noise_direction']
                val = LINE_NOISE_DIRECTIONS.get(d.lower() if isinstance(d, str) else d, str(d))
                patch_dict.setdefault('RAW Bayer', {})['LineDenoiseDirection'] = val
            if 'green_equilibration' in rp:
                patch_dict.setdefault('RAW Bayer', {})['GreenEqThreshold'] = str(rp['green_equilibration'])

        if 'raw_calibration' in effective_adj:
            rc = effective_adj['raw_calibration']
            if 'black_offsets' in rc:
                bo = rc['black_offsets']
                bayer_sec = patch_dict.setdefault('RAW Bayer', {})
                bayer_sec['Dehablack'] = 'false'
                if 'green1' in bo:
                    v = bo['green1']
                    bayer_sec['PreBlack0'] = str(int(v)) if isinstance(v, int) or (isinstance(v, float) and v.is_integer()) else f"{v:.8g}"
                if 'red' in bo:
                    v = bo['red']
                    bayer_sec['PreBlack1'] = str(int(v)) if isinstance(v, int) or (isinstance(v, float) and v.is_integer()) else f"{v:.8g}"
                if 'blue' in bo:
                    v = bo['blue']
                    bayer_sec['PreBlack2'] = str(int(v)) if isinstance(v, int) or (isinstance(v, float) and v.is_integer()) else f"{v:.8g}"
                if 'green2' in bo:
                    v = bo['green2']
                    bayer_sec['PreBlack3'] = str(int(v)) if isinstance(v, int) or (isinstance(v, float) and v.is_integer()) else f"{v:.8g}"

                if 'green1' in bo and 'green2' in bo:
                    bayer_sec['PreTwoGreen'] = 'true' if bo['green1'] == bo['green2'] else 'false'
                elif 'green2' in bo:
                    bayer_sec['PreTwoGreen'] = 'false'

            if 'white_scale' in rc:
                ws = rc['white_scale']
                ws_val = str(int(ws)) if isinstance(ws, int) or (isinstance(ws, float) and ws.is_integer()) else f"{ws:.8g}"
                patch_dict.setdefault('RAW', {})['PreExposure'] = ws_val

        parent = Path(req['output_dir']).expanduser().resolve()
        disk_preflight = check_render_space(parent)
        parent.mkdir(parents=True, exist_ok=True)
        identity = json.dumps({'source': src_hash, 'profile_sha': meta['sha256'],
                               'effective_adj': effective_adj, 'engine': rt,
                               'format': req['format'], 'output_profile': output_icc,
                               'preview_metadata': req.get('preview_metadata', 'strip'), 'tool': VERSION}, sort_keys=True).encode()
        tag = hashlib.sha256(identity).hexdigest()[:16]
        folder = Path(tempfile.mkdtemp(prefix=f'{source.stem}__rt-{rt["version"]}__{tag}-', dir=parent))
        active_lock = FileLock(folder / '.active.lock')
        active_lock.acquire(non_blocking=False)

        snapshots_dir = folder / 'snapshots'
        external_resources = {}

        if 'lens_correction' in effective_adj:
            lc = effective_adj['lens_correction']
            lc_sec = patch_dict.setdefault('LensProfile', {})
            mode_map = {
                'none': 'none',
                'lensfun_auto': 'lfauto',
                'lensfun_manual': 'lfmanual',
                'lcp': 'lcp'
            }
            if 'mode' in lc:
                lc_sec['LcMode'] = mode_map[lc['mode']]
            if 'distortion' in lc:
                lc_sec['UseDistortion'] = 'true' if lc['distortion'] else 'false'
            if 'vignetting' in lc:
                lc_sec['UseVignette'] = 'true' if lc['vignetting'] else 'false'
            if 'ca' in lc:
                lc_sec['UseCA'] = 'true' if lc['ca'] else 'false'
            if lc.get('mode') == 'lensfun_manual':
                lc_sec['LFCameraMake'] = lc['camera_make']
                lc_sec['LFCameraModel'] = lc['camera_model']
                lc_sec['LFLens'] = lc['lens']
            elif lc.get('mode') == 'lcp':
                lcp_orig = file_path(lc['path'])
                require(lcp_orig.is_file(), 'FILE_NOT_FOUND', f'LCP file not found: {lcp_orig}')
                lcp_sha = digest(lcp_orig)
                require(lcp_sha == lc['sha256'], 'CHECKSUM_MISMATCH',
                        f'LCP file hash mismatch: expected {lc["sha256"]}, got {lcp_sha}')
                snapshots_dir.mkdir(parents=True, exist_ok=True)
                lens_snapshot_path = snapshots_dir / f'lens_{lcp_sha[:16]}.lcp'
                shutil.copy2(lcp_orig, lens_snapshot_path)
                require(digest(lens_snapshot_path) == lcp_sha, 'IO_ERROR', 'Failed to snapshot LCP file')
                lc_sec['LCPFile'] = str(lens_snapshot_path.resolve())
                external_resources['lens_profile'] = {
                    'canonical_path': str(lcp_orig.resolve()),
                    'sha256': lcp_sha,
                    'snapshot_path': str(lens_snapshot_path.resolve()),
                    'snapshot_sha256': lcp_sha,
                    'effect_verified': False,
                    'verification_status': 'resource_integrity_verified_effect_unverified'
                }

        if 'input_profile' in effective_adj:
            ip = effective_adj['input_profile']
            ip_sec = patch_dict.setdefault('Color Management', {})
            if ip.get('mode') == 'camera_standard':
                ip_sec['InputProfile'] = '(camera)'
            elif ip.get('mode') == 'auto_matched':
                ip_sec['InputProfile'] = '(cameraICC)'
            elif ip.get('mode') == 'custom':
                ip_orig = file_path(ip['path'])
                require(ip_orig.is_file(), 'FILE_NOT_FOUND', f'Input profile file not found: {ip_orig}')
                ip_sha = digest(ip_orig)
                require(ip_sha == ip['sha256'], 'CHECKSUM_MISMATCH',
                        f'Input profile file hash mismatch: expected {ip["sha256"]}, got {ip_sha}')
                snapshots_dir.mkdir(parents=True, exist_ok=True)
                ext = ip_orig.suffix.lower()
                input_snapshot_path = snapshots_dir / f'input_{ip_sha[:16]}{ext}'
                shutil.copy2(ip_orig, input_snapshot_path)
                require(digest(input_snapshot_path) == ip_sha, 'IO_ERROR', 'Failed to snapshot input profile file')
                ip_sec['InputProfile'] = str(input_snapshot_path.resolve())
                external_resources['input_profile'] = {
                    'canonical_path': str(ip_orig.resolve()),
                    'sha256': ip_sha,
                    'snapshot_path': str(input_snapshot_path.resolve()),
                    'snapshot_sha256': ip_sha,
                    'effect_verified': False,
                    'verification_status': 'resource_integrity_verified_effect_unverified'
                }

            if 'tone_curve' in ip:
                ip_sec['ToneCurve'] = 'true' if ip['tone_curve'] else 'false'
            if 'look_table' in ip:
                ip_sec['ApplyLookTable'] = 'true' if ip['look_table'] else 'false'
            if 'hue_sat_map' in ip:
                ip_sec['ApplyHueSatMap'] = 'true' if ip['hue_sat_map'] else 'false'
            if 'baseline_exposure' in ip:
                ip_sec['ApplyBaselineExposureOffset'] = 'true' if ip['baseline_exposure'] else 'false'
            if 'dcp_illuminant' in ip:
                ip_sec['DCPIlluminant'] = str(ip['dcp_illuminant'])

        if output_icc:
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            snapshot = snapshots_dir / ('output_' + output_icc['sha256'][:16] + '.icc')
            require(not any(c in str(snapshot) for c in ('\n', '\r')), 'INVALID_REQUEST', 'Output ICC snapshot path must be single-line')
            shutil.copy2(output_icc['canonical_path'], snapshot)
            require(digest(snapshot) == output_icc['sha256'], 'CHECKSUM_MISMATCH', 'Output ICC changed while snapshotting')
            patch_dict.setdefault('Color Management', {})['OutputProfile'] = 'file:' + str(snapshot)
            external_resources['output_profile'] = {**output_icc, 'snapshot_path': str(snapshot), 'snapshot_sha256': digest(snapshot)}

        if patch_dict:
            text = apply_pp3_patches(text, patch_dict)
        work = Path(tempfile.mkdtemp(prefix='.work-', dir=folder))
        pp3 = work / 'effective.pp3'
        pp3.write_text(text, encoding='utf-8')
        output = work / ('image.jpg' if req['format'] == 'jpeg' else 'image.tif')
        argv = [rt['path'], '-q', '-p', str(pp3), '-o', str(output)]
        argv += ['-j95', '-js3'] if req['format'] == 'jpeg' else ['-t', '-b16']
        argv += ['-c', str(source)]
        run_options = {'timeout': req.get('timeout_seconds', RENDER_TIMEOUT_RULE['default']), 'settings': work / 'settings'}
        if 'threads' in req:
            run_options['threads'] = req['threads']
        log = run(argv, **run_options)
        stats = qc(output, preview_path=work / 'preview.jpg', preview_metadata=req.get('preview_metadata', 'strip'))
        preview = stats.pop('preview')
        require(stats['icc_sha256'], 'OUTPUT_INVALID', 'Output missing embedded ICC')
        if output_icc:
            require(stats['icc_sha256'] == output_icc['sha256'], 'OUTPUT_PROFILE_MISMATCH',
                    'Rendered ICC differs from the requested profile; refusing fallback or silent profile reassignment')
        export_info = {
            'output_profile': output_icc,
            'embedded_icc_description': stats['icc_description'], 'embedded_icc_sha256': stats['icc_sha256'],
            'requested_icc_verified': output_icc is not None,
            'preview_metadata': req.get('preview_metadata', 'strip'),
            'omp_num_threads': str(req['threads']) if 'threads' in req else os.environ.get('OMP_NUM_THREADS'),
            'omp_thread_limit': str(req['threads']) if 'threads' in req else os.environ.get('OMP_THREAD_LIMIT'),
            'thread_policy_source': 'request' if 'threads' in req else 'environment_or_engine_default',
            'effective_thread_count_verified': False
        }
        warnings = render_warnings(effective_adj, req['format'], stats['icc_description'])
        require(digest(source) == src_hash, 'SOURCE_CHANGED', 'Source changed during render')

        pp3.rename(folder / pp3.name)
        output.rename(folder / output.name)
        (work / 'preview.jpg').rename(folder / 'preview.jpg')
        preview['path'] = str(folder / 'preview.jpg')
        (folder / 'engine.log').write_text(log, encoding='utf-8')

        eff_pp3_hash = digest(folder / pp3.name)
        output_hash = digest(folder / output.name)

        manifest = {'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': VERSION, 'request': req,
                    'engine': rt, 'source_type': src_info['source_type'], 'source_sha256': src_hash,
                    'profile': meta,
                    'effective_pp3_sha256': eff_pp3_hash, 'output_sha256': output_hash,
                    'effective_adjustments': effective_adj,
                    'argv': argv, 'qc': stats, 'preview': preview, 'disk_preflight': disk_preflight, 'export': export_info,
                    'warnings': warnings, 'visual_review': 'pending',
                    'commit_status': 'reference' if has_session else 'unbound',
                    'commit_authority': 'session_history' if has_session else 'manifest',
                    'replay_note': 'Keep original source file and external ICC/DCP/lens assets; hashes alone cannot recreate them.',
                    'external_resources': external_resources,
                    'pipeline_stages': {
                        'requested': True,
                        'serialized': True,
                        'render_completed': True,
                        'effect_verified': False
                    },
                    'verification': {
                        'effect_verified': False,
                        'lens_correction': {
                            'mode': effective_adj.get('lens_correction', {}).get('mode', 'none') if 'lens_correction' in effective_adj else 'none',
                            'serialized': 'lens_correction' in effective_adj,
                            'effect_verified': False,
                            'lensfun_match_status': 'unknown' if effective_adj.get('lens_correction', {}).get('mode', '').startswith('lensfun') else None,
                            'lcp_file_integrity': 'verified' if effective_adj.get('lens_correction', {}).get('mode') == 'lcp' else None,
                            'optical_correction_verified': False
                        },
                        'input_profile': {
                            'mode': effective_adj.get('input_profile', {}).get('mode', 'none') if 'input_profile' in effective_adj else 'none',
                            'serialized': 'input_profile' in effective_adj,
                            'effect_verified': False,
                            'camera_match_status': 'unknown',
                            'profile_integrity': 'verified' if effective_adj.get('input_profile', {}).get('mode') == 'custom' else None,
                            'color_accuracy_verified': False
                        }
                    }}
        if src_info['source_type'] == 'jpeg':
            manifest['source_has_icc'] = src_info.get('has_icc')
            manifest['source_icc_description'] = src_info.get('icc_description')
            manifest['source_orientation'] = src_info.get('orientation')

        if has_session:
            manifest['session_id'] = sess_id
            manifest['input_revision'] = exp_rev
            manifest['intent'] = intent_snapshot

        manifest_path = folder / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest_hash = digest(manifest_path)
        manifest_complete = True

        shutil.rmtree(work)

        # Commit session if bound: Short Lock 2
        if has_session:
            with FileLock(sess_lock_path):
                try:
                    raw_latest = sess_path.read_text(encoding='utf-8')
                    latest_data = json.loads(raw_latest)
                except json.JSONDecodeError as e:
                    raise ToolError('CORRUPT_SESSION', f'Session file contains invalid JSON: {e}')
                except Exception as e:
                    raise ToolError('CORRUPT_SESSION', f'Cannot read session file: {e}')

                require(isinstance(latest_data, dict), 'CORRUPT_SESSION', 'Session file content must be a JSON object')
                ver = latest_data.get('schema_version')
                require(ver == 1, 'UNSUPPORTED_SESSION_SCHEMA',
                        f'Unsupported session schema_version: {ver}; expected 1. Do not auto-reset.')

                # Check session_id / binding identity
                latest_sess_id = latest_data.get('session_id')
                require(latest_sess_id == sess_id, 'SESSION_ID_MISMATCH',
                        f'Session ID changed: expected {sess_id}, but found {latest_sess_id}')

                # CAS revision check
                cur_rev = latest_data.get('revision', 0)
                require(cur_rev == exp_rev, 'REVISION_CONFLICT',
                        f'Session revision conflict: expected {exp_rev}, but file is at {cur_rev}. Reload before updating.')

                now = datetime.now(timezone.utc).isoformat()
                history_entry = {
                    'revision': exp_rev,
                    'committed_revision': exp_rev + 1,
                    'timestamp': now,
                    'adjustments': dict(effective_adj),
                    'manifest_path': str(manifest_path),
                    'manifest_sha256': manifest_hash,
                    'output_path': str(folder / output.name),
                    'output_sha256': output_hash,
                    'preview_path': preview['path'],
                    'preview_sha256': preview['sha256'],
                    'export': export_info,
                    'status': 'SUCCESS'
                }
                latest_data.setdefault('history', []).append(history_entry)
                latest_data['effective_adjustments'] = effective_adj
                latest_data['revision'] = exp_rev + 1
                latest_data['updated_at'] = now

                try:
                    atomic_write_json(sess_path, latest_data)
                except Exception as e:
                    raise ToolError('SESSION_COMMIT_FAILED', f'Render output generated at {folder / output.name} but session commit failed: {e}')

        return {'output': str(folder / output.name), 'manifest': str(manifest_path),
                'source_type': src_info['source_type'], 'qc': stats, 'preview': preview, 'export': export_info,
                'warnings': warnings,
                'session_revision': exp_rev + 1 if has_session else None,
                'pipeline_stages': {
                    'requested': True, 'serialized': True, 'render_completed': True, 'effect_verified': False
                },
                'verification': {
                    'effect_verified': False,
                    'lens_correction_effect_verified': False,
                    'input_profile_effect_verified': False
                }}
    except Exception as exc:
        if has_session and manifest_complete and isinstance(exc, ToolError):
            exc.details = {'uncommitted_render': {'output': str(folder / output.name),
                           'manifest': str(manifest_path), 'preview': preview,
                           'input_revision': exp_rev, 'status': 'uncommitted'},
                           'recovery': 'Reload the latest session before rendering again; this artifact is not committed.'}
        cleanup_note = ''
        if folder is not None and (not has_session or not manifest_complete):
            try:
                shutil.rmtree(folder)
            except OSError as cleanup_exc:
                cleanup_note = f'; cleanup incomplete at {folder}: {cleanup_exc}'
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise ToolError('INSUFFICIENT_DISK_SPACE', str(exc) + cleanup_note) from exc
        if cleanup_note:
            code = exc.code if isinstance(exc, ToolError) else 'INVALID_OR_FAILED'
            raise ToolError(code, str(exc) + cleanup_note, getattr(exc, 'details', None)) from exc
        raise
    finally:
        if active_lock:
            active_lock.release()
        if render_gate:
            render_gate.release()

def exiftool_hint():
    if platform.system() == 'Darwin':
        return 'ExifTool not found in PATH. If Homebrew is installed, run: brew install exiftool; then verify: exiftool -ver. No installation was performed.'
    return 'ExifTool not found in PATH. Install ExifTool with your system package manager or from https://exiftool.org, then verify: exiftool -ver. No installation was performed.'

def render_warnings(adjustments, output_format=None, icc_description=None):
    warnings = []
    lens = adjustments.get('lens_correction', {})
    if lens.get('mode', '').startswith('lensfun'):
        warnings.append({'code': 'LENSFUN_MATCH_UNVERIFIED',
                         'message': '已请求 Lensfun 校正，但引擎匹配与实际效果尚未验证。可先用 inspect 的 lens_check 查看名称候选；不能仅凭渲染成功宣称校正已应用。'})
    if lens.get('mode') == 'lcp' and 'path' in lens:
        info = bundled_lcp_info(lens['path'])
        if info and not info['recommended']:
            warnings.append({'code': 'UNVERIFIED_LENS_PROFILE', 'alias': info['alias'],
                             'message': '此内置 LCP 缺少光学标定依据，仅供显式实验；不推荐用于正式校正，渲染成功不代表光学准确。'})
    # Description matching is a delivery hint, not proof of ICC colorimetry or viewer support.
    common_srgb = {'srgb', 'srgb built-in', 'srgb iec61966-2.1', 'rtv2_srgb', 'rtv4_srgb'}
    if output_format == 'jpeg' and (icc_description or '').strip().casefold() not in common_srgb:
        warnings.append({'code': 'JPEG_DELIVERY_PROFILE_REVIEW', 'icc_description': icc_description,
                         'message': '此 JPEG 的嵌入 ICC 未被识别为常用 sRGB 配置。接收端忽略或移除 ICC 时可能偏色、失色；sRGB 预览正常不证明主成片在该端显示一致。日常分享优先重新显影为 sRGB；保留此文件时请使用支持该 ICC 的接收端。此提示按描述识别，不证明色域或客户端能力。'})
    return warnings

def local_lensfun_roots(engine_path):
    executable = Path(engine_path).resolve()
    bundle = next((p for p in executable.parents if p.name == 'Contents' and p.parent.suffix == '.app'), None)
    roots = [bundle / 'Resources/share/lensfun'] if bundle else [executable.parent.parent / 'share/lensfun']
    return [p for p in roots if p.is_dir()]

def lens_support_check(metadata):
    """Exact-name XML evidence only; deliberately not a replacement Lensfun matcher."""
    import xml.etree.ElementTree as ET
    base = {'engine_match_status': 'unknown', 'engine_database_verified': False,
            'optical_correction_verified': False, 'mount_compatibility_verified': False}
    row = metadata[0] if isinstance(metadata, list) and metadata and isinstance(metadata[0], dict) else {}
    name = next((row[k].strip() for k in ('LensModel', 'Lens', 'LensID')
                 if isinstance(row.get(k), str) and row[k].strip()
                 and row[k].strip().lower() not in {'unknown', 'none', '0'}), None)
    if not name:
        return {**base, 'status': 'missing_metadata', 'hint': '缺少可识别的镜头名称，无法预检 Lensfun 名称候选；自动校正是否应用仍未知。'}
    normalized = lambda text: ' '.join(text.split()).casefold()
    builtin_note = ''
    alias = BUILTIN_LENS_MAP.get(normalized(name))
    if alias:
        try:
            builtin, _ = profile(alias, kind='lcp')
            info = builtin['builtin_profile']
            base['builtin_profile_status'] = info['calibration_status']
            if info['recommended']:
                base['builtin_profile_available'] = alias
                builtin_note = f' 内置配置候选：{alias}；仍需核对适用条件和实际效果。'
            else:
                base['experimental_profile_available'] = alias
                builtin_note = f' 发现未标定的实验 LCP：{alias}；不可自动推荐或作为准确校正使用。'
        except (ToolError, OSError) as exc:
            base['builtin_profile_status'] = 'unavailable'
            base['builtin_profile_error'] = str(exc)
            builtin_note = ' 内置 LCP 缺失、不可读或完整性检查失败，不能使用。'
    try:
        eng = engine()
        roots = local_lensfun_roots(eng['path'])
    except (ToolError, OSError) as exc:
        return {**base, 'status': 'database_unavailable', 'query_name': name,
                'hint': f'无法定位待检查的 Lensfun 库：{exc}。不能据此判断镜头支持情况。' + builtin_note}
    files = sorted({p for root in roots for p in root.rglob('*.xml')})
    if not files:
        return {**base, 'status': 'database_unavailable', 'query_name': name,
                'hint': '在引擎相邻位置未找到可扫描的 Lensfun XML；不代表镜头不受支持。' + builtin_note}
    matches, errors = [], []
    for path in files:
        try:
            tree = ET.parse(path).getroot()
            if tree.tag != 'lensdatabase':
                raise ValueError('Unexpected XML root')
            for lens in tree.findall('lens'):
                maker = lens.findtext('maker', '').strip()
                models = [n.text.strip() for n in lens.findall('model') if n.text and n.text.strip()]
                if any(normalized(name) in {normalized(m), normalized(maker + ' ' + m)} for m in models):
                    matches.append({'maker': maker, 'model': models[0], 'database_file': str(path),
                                    'mounts': [n.text for n in lens.findall('mount')],
                                    'calibration_entries': {k: lens.find('.//' + k) is not None
                                                            for k in ('distortion', 'vignetting', 'tca')}})
        except (OSError, ET.ParseError, ValueError) as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    status = 'exact_name_candidate' if matches else ('incomplete_scan' if errors else 'not_found_in_scanned_database')
    hint = ('找到精确名称候选；机身/卡口兼容性、引擎匹配及校正是否应用仍未验证。' if matches else
            '库扫描不完整，无法判断名称是否缺失。' if errors else
            '扫描库中未找到精确名称候选，可能是名称差异或缺少条目；请核对 GUI 的机身/镜头匹配，或提供适配的 LCP。不能假定 lensfun_auto 已生效。')
    result = {**base, 'status': status, 'query_name': name, 'hint': hint + builtin_note,
              'engine_path': eng['path'], 'database_roots': [str(p) for p in roots],
              'scanned_file_count': len(files), 'scan_complete': not errors,
              'candidate_count': len(matches), 'candidates': matches[:5],
              'parse_error_count': len(errors), 'parse_errors': errors[:5]}
    return result

def adjustment_parameters():
    """Shared public parameter definitions for capabilities, schema and preflight."""
    return {
        'exposure_ev': {'type': 'number', 'minimum': -2.0, 'maximum': 2.0, 'step': 0.05, 'default': 0.0},
        'contrast': {'type': 'integer', 'minimum': -100, 'maximum': 100, 'step': 1, 'default': 0},
        'saturation': {'type': 'integer', 'minimum': -100, 'maximum': 100, 'step': 1, 'default': 0},
        'white_balance': {
            'temperature': {
                'type': 'integer',
                'minimum': 1500,
                'maximum': 60000,
                'step': 5,
                'desc': 'RawTherapee color temperature control. RAW有更多白平衡调整信息也不自动保证精准校色。JPEG参数为RawTherapee对已烘焙图像的颜色调整控制；相对当前试片调整冷暖与偏色，需预览确认，数值不代表重新设定拍摄白平衡，5500不保证中性。'
            },
            'green': {
                'type': 'number',
                'minimum': 0.5,
                'maximum': 2.0,
                'step': 0.001,
                'default': 1.0,
                'desc': 'tint multiplier: <1.0 magenta bias, >1.0 green bias'
            }
        },
        'tone_curve': {
            'modes': ['Standard', 'FilmLike', 'Perceptual'],
            'points': {'min_points': 2, 'max_points': 16, 'x_range': [0.0, 1.0], 'y_range': [0.0, 1.0], 'desc': 'x strictly increasing starting at 0.0 ending at 1.0; y non-descending'}
        },
        'denoise': {
            'enabled': {'type': 'boolean', 'default': True},
            'luma': {'type': 'number', 'minimum': 0.0, 'maximum': 100.0, 'step': 0.01, 'default': 0.0},
            'chroma': {'type': 'number', 'minimum': 0.0, 'maximum': 100.0, 'step': 0.01, 'default': 15.0}
        },
        'sharpen': {
            'enabled': {'type': 'boolean', 'default': True},
            'amount': {'type': 'integer', 'minimum': 10, 'maximum': 500, 'step': 1, 'default': 200},
            'radius': {'type': 'number', 'minimum': 0.3, 'maximum': 3.0, 'step': 0.01, 'default': 0.5}
        },
        'highlight_recovery': {
            'enabled': {'type': 'boolean', 'default': False, 'desc': 'RawTherapee 5.13 highlight reconstruction (HLRecovery.Enabled). RAW only.'},
            'method': {
                'type': 'string',
                'enum': ['luminance', 'cielab', 'blend', 'color', 'coloropp'],
                'desc': 'Reconstruction method (HLRecovery.Method): Luminance, CIELab blending, Blend, Color, Coloropp. Choose explicitly; no universal default. Compare alternatives for colored clipped lights; luminance is neutral gray, not recovered original color. RAW only.'
            }
        },
        'highlight_compression': {
            'amount': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 500,
                'step': 1,
                'default': 0,
                'desc': 'Highlight compression amount (Exposure.HighlightCompr). Supported on RAW and JPEG tone curve roll-off.'
            },
            'threshold': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 100,
                'step': 1,
                'default': 0,
                'desc': 'Highlight compression threshold (Exposure.HighlightComprThreshold). Supported on RAW and JPEG.'
            }
        },
        'demosaic': {
            'method': {
                'type': 'string',
                'enum': sorted(list(BAYER_DEMOSAIC_METHODS)),
                'default': 'amaze',
                'desc': 'RawTherapee 5.13 Bayer demosaicing method (RAW Bayer.Method). Bayer RAW only (e.g. Nikon NEF). Dual methods: amazebilinear, amazevng4, rcdbilinear, rcdvng4, dcbbilinear, dcbvng4. Does not apply to Fujifilm X-Trans sensors.'
            },
            'dual_contrast_auto': {
                'type': 'boolean',
                'default': True,
                'desc': 'Auto contrast threshold (RAW Bayer.DualDemosaicAutoContrast). Explicit true removes an inherited manual threshold. Conflicts with dual_contrast in the same patch.'
            },
            'dual_contrast': {
                'type': 'number',
                'minimum': 0.0,
                'maximum': 100.0,
                'default': 20.0,
                'desc': 'Manual contrast threshold (RAW Bayer.DualDemosaicContrast). Supplying a value implies dual_contrast_auto=false unless explicitly specified.'
            }
        },
        'raw_preprocess': {
            'hot_pixel_filter': {'type': 'boolean', 'default': False, 'desc': 'Hot pixel suppression filter (RAW.HotPixelFilter). RAW only.'},
            'dead_pixel_filter': {'type': 'boolean', 'default': False, 'desc': 'Dead pixel suppression filter (RAW.DeadPixelFilter). RAW only.'},
            'hot_dead_pixel_threshold': {
                'type': 'integer', 'minimum': 20, 'maximum': 200, 'default': 100,
                'desc': 'Threshold for hot/dead pixel detection (RAW.HotDeadPixelThresh).'
            },
            'line_noise': {
                'type': 'integer', 'minimum': 0, 'maximum': 1000, 'default': 0,
                'desc': 'Bayer line noise filter strength (RAW Bayer.LineDenoise).'
            },
            'line_noise_direction': {
                'type': 'string', 'enum': ['horizontal', 'vertical', 'both'], 'default': 'both',
                'desc': 'Direction for line denoise (RAW Bayer.LineDenoiseDirection): 1=horizontal, 2=vertical, 3=both.'
            },
            'green_equilibration': {
                'type': 'integer', 'minimum': 0, 'maximum': 100, 'default': 0,
                'desc': 'Green equilibration threshold (RAW Bayer.GreenEqThreshold).'
            }
        },
        'raw_calibration': {
            'black_offsets': {
                'type': 'object',
                'desc': 'RawTherapee Bayer sensor black level offsets (RAW Bayer.PreBlack0..3, in [-2048, 2048]; relative offset, not absolute sensor BlackLevel). Nikon .nef only.',
                'properties': {
                    'red': {'type': 'number', 'minimum': -2048.0, 'maximum': 2048.0, 'default': 0.0, 'desc': 'Red black offset (PreBlack1)'},
                    'green1': {'type': 'number', 'minimum': -2048.0, 'maximum': 2048.0, 'default': 0.0, 'desc': 'Green 1 lead black offset (PreBlack0)'},
                    'blue': {'type': 'number', 'minimum': -2048.0, 'maximum': 2048.0, 'default': 0.0, 'desc': 'Blue black offset (PreBlack2)'},
                    'green2': {'type': 'number', 'minimum': -2048.0, 'maximum': 2048.0, 'default': 0.0, 'desc': 'Green 2 black offset (PreBlack3). Independent from green1 (sets PreTwoGreen=false when different).'}
                }
            },
            'white_scale': {
                'type': 'number',
                'minimum': 0.1,
                'maximum': 16.0,
                'default': 1.0,
                'desc': 'Raw linear white point scaling multiplier (RAW.PreExposure, in [0.1, 16.0]; not absolute WhiteLevel). Nikon .nef only.'
            }
        },
        'lens_correction': {
            'type': 'object',
            'desc': 'RawTherapee 5.13 lens profile correction (LensProfile). RAW only.',
            'properties': {
                'mode': {'type': 'string', 'enum': ['none', 'lensfun_auto', 'lensfun_manual', 'lcp'],
                         'desc': 'LcMode. metadata mode is unsupported for Nikon NEF in RT 5.13 and rejected.'},
                'distortion': {'type': 'boolean', 'desc': 'Enable distortion correction (UseDistortion)'},
                'vignetting': {'type': 'boolean', 'desc': 'Enable vignette correction (UseVignette)'},
                'ca': {'type': 'boolean', 'desc': 'Enable chromatic aberration correction (UseCA)'},
                'camera_make': {'type': 'string', 'desc': 'Camera maker for lensfun_manual (LFCameraMake)'},
                'camera_model': {'type': 'string', 'desc': 'Camera model for lensfun_manual (LFCameraModel)'},
                'lens': {'type': 'string', 'desc': 'Lens model for lensfun_manual (LFLens)'},
                'path': {'type': 'string', 'desc': 'Adobe LCP path or supported builtin:lcp alias. Alias resolves using explicit or inherited lcp mode; unverified bundled profiles are experimental only.'},
                'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$', 'desc': 'SHA-256 digest of LCP profile; omitted hash is filled only when explicitly binding a supported builtin alias.'}
            },
            'verification_level': 'file_integrity_only'
        },
        'input_profile': {
            'type': 'object',
            'desc': 'RawTherapee 5.13 input color profile and DCP controls (Color Management). RAW only.',
            'properties': {
                'mode': {'type': 'string', 'enum': ['camera_standard', 'auto_matched', 'custom'],
                         'desc': 'camera_standard maps to (camera); auto_matched maps to (cameraICC); custom maps to external DCP/ICC/ICM file.'},
                'path': {'type': 'string', 'desc': 'Path to custom DCP/ICC/ICM file (extension .dcp, .icc, .icm)'},
                'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$', 'desc': 'SHA-256 digest of profile file'},
                'tone_curve': {'type': 'boolean', 'desc': 'Apply DCP embedded tone curve (ToneCurve)'},
                'look_table': {'type': 'boolean', 'desc': 'Apply DCP look table (ApplyLookTable)'},
                'hue_sat_map': {'type': 'boolean', 'desc': 'Apply DCP hue/saturation/lightness map (ApplyHueSatMap)'},
                'baseline_exposure': {'type': 'boolean', 'desc': 'Apply DCP baseline exposure offset (ApplyBaselineExposureOffset)'},
                'dcp_illuminant': {'type': 'integer', 'enum': [0, 1, 2], 'desc': 'DCP illuminant selection: 0=auto (interpolated based on white balance), 1=first calibration illuminant, 2=second calibration illuminant'}
            },
            'verification_level': 'file_integrity_only'
        }
    }

def enum_schema(values):
    # Portable JSON Schema regex; no engine-specific inline regex flags.
    pattern = '^(?:' + '|'.join(''.join(
        '[' + ch.lower() + ch.upper() + ']' if ch.isalpha() else re.escape(ch)
        for ch in value) for value in values) + ')$(?![\\s\\S])'
    return {'type': 'string', 'pattern': pattern, 'examples': values,
            'x-canonical-enum': values}

def adjustments_schema():
    """Schema for incremental patches; effective-state requirements run after merge."""
    def field_schema(spec):
        if 'type' not in spec:
            spec = {'type': 'object', 'properties': spec}
        rule = {k: copy.deepcopy(v) for k, v in spec.items()
                if k in {'type', 'minimum', 'maximum', 'enum', 'pattern'}}
        if 'desc' in spec:
            rule['description'] = spec['desc']
        if rule['type'] == 'object':
            rule.update(properties={k: field_schema(v) for k, v in spec['properties'].items()},
                        additionalProperties=False, minProperties=1)
        elif rule['type'] == 'string':
            rule['minLength'] = 1
            if 'enum' in rule:
                values = rule.pop('enum')
                rule.update(enum_schema(values))
        return rule

    parameters = adjustment_parameters()
    curve = parameters.pop('tone_curve')
    props = {k: field_schema(v) for k, v in parameters.items()}
    props['demosaic']['not'] = {
        'type': 'object', 'required': ['dual_contrast', 'dual_contrast_auto'],
        'properties': {'dual_contrast_auto': {'enum': [True]}}
    }
    props['tone_curve'] = {
        'type': 'object', 'minProperties': 1, 'additionalProperties': False,
        'properties': {
            'mode': enum_schema(curve['modes']),
            'points': {
                'type': 'array', 'minItems': 2, 'maxItems': 16,
                'items': {'type': 'array', 'minItems': 2, 'maxItems': 2,
                          'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
                'description': curve['points']['desc']
            }
        }
    }
    return {
        'type': 'object', 'properties': props, 'additionalProperties': False,
        'description': 'Incremental patch. Omitted fields inherit session state or Base PP3. '
                       'Completeness, curve ordering, source compatibility and resource checks run after merge. '
                       'dual_contrast implies dual_contrast_auto=false when omitted; '
                       'explicit auto=true plus a threshold is invalid.'
    }

def capabilities(req):
    rt = None
    try:
        rt = engine()
        ver = rt['version']
    except ToolError:
        ver = 'unknown'
    src_type = req.get('source_type')
    caps = {
        'tool_version': VERSION,
        'engine_version': ver,
        'supported_actions': ['doctor', 'capabilities', 'inspect', 'profile', 'render', 'qc', 'session', 'clean', 'raw_qc', 'prune_history'],
        'controllable_parameters': adjustment_parameters(),
        'enum_input_policy': 'Adjustment string enum values are case-insensitive and persisted in canonical form; field names and action/operation values remain case-sensitive.',
        'export_parameters': copy.deepcopy({k: VARIANT_RENDER['properties'][k] for k in ('output_profile', 'preview_metadata', 'threads')}),
        'output_profiles': local_output_profiles(rt['path']) if rt and 'path' in rt else [],
        'builtin_profiles': sorted(BUNDLED_PROFILES),
        'builtin_profile_metadata': copy.deepcopy(BUNDLED_LCP_METADATA),
        'source_restrictions': {
            'raw': 'Raw sensor data pipeline supporting highlight recovery, highlight compression, raw sensor preprocessing, raw calibration, lens correction (lensfun_auto, lensfun_manual, lcp), and input color profiles (camera_standard, auto_matched, custom DCP/ICC/ICM). Bayer-exclusive controls (demosaic, line denoise, green equilibration, raw calibration) are strictly restricted to Nikon .nef Bayer sources in this version. raw_qc provides unrotated visible CFA raw sensor code diagnostics.',
            'jpeg': 'Baked 8-bit non-linear RGB; tone curve cannot recover clipped whites (255,255,255); highlight_compression is supported for highlight tone curve roll-off but is not RAW highlight recovery; no demosaicing, RAW highlight recovery, raw sensor preprocessing, raw calibration, lens correction, or camera input profiles.'
        },
        'unsupported_capabilities': [
            'local_mask_brush',
            'generative_object_removal',
            'artistic_guarantee_for_style_labels'
        ]
    }
    if src_type:
        caps['filtered_for_source_type'] = src_type
        caps['effective_restriction'] = caps['source_restrictions'][src_type]
    available = set(ALLOWED_ADJUSTMENT_CATEGORIES)
    if src_type == 'jpeg':
        available -= RAW_ONLY_ADJUSTMENTS
    requested = set(req.get('parameters', available))
    require(requested <= available, 'UNSUPPORTED_ADJUSTMENTS',
            f'Parameters unsupported for source_type={src_type}: {sorted(requested - available)}')
    caps['controllable_parameters'] = {k: v for k, v in caps['controllable_parameters'].items() if k in requested}
    caps['available_adjustments'] = sorted(available)
    caps['structured_adjustments_available'] = ver == '5.13'
    caps['runtime_limits'] = {'recommended_concurrent_renders': 1, 'cross_session_throttle_enforced': False,
                             'minimum_output_free_bytes': MIN_RENDER_FREE_BYTES,
                             'render_timeout_seconds': dict(RENDER_TIMEOUT_RULE)}
    if req.get('detail', 'full') == 'summary':
        caps.pop('controllable_parameters')
        caps['available_adjustments'] = sorted(requested)
        caps['parameter_help'] = 'Use detail=full and parameters=[category,...] for only the controls needed this round.'
        if src_type:
            caps['source_restrictions'] = {src_type: caps['source_restrictions'][src_type]}
    return caps

class FileLock:
    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        self.fd = None

    def acquire(self, non_blocking=False):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        flags = fcntl.LOCK_EX
        if non_blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.fd, flags)
        except (BlockingIOError, OSError):
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        ok = self.acquire(non_blocking=False)
        if not ok:
            raise ToolError('LOCK_BUSY', f'Could not acquire lock: {self.lock_path}')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

def atomic_write_json(target_path, data):
    target = Path(target_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix='.session-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

def session_init(req):
    require('reset_adjustments' not in req and 'reset_fields' not in req,
            'INVALID_REQUEST', 'reset parameters are not supported in session init')
    target = Path(req['session_path']).expanduser().resolve()
    require(not target.exists(), 'SESSION_EXISTS',
            f'Session file already exists: {target}; init refuses to overwrite')

    require('source' in req, 'INVALID_REQUEST', 'source is required for session init')
    require('base_profile' in req, 'INVALID_REQUEST', 'base_profile is required for session init')
    require('confirmed_brief' in req, 'INVALID_REQUEST', 'confirmed_brief is required for session init')

    source = file_path(req['source'])
    src_info = check_source(source)
    src_hash = digest(source)

    prof_meta, _ = profile(req['base_profile'])
    prof_hash = prof_meta['sha256']

    brief = req['confirmed_brief']
    require(isinstance(brief, str) and bool(brief.strip()) and len(brief) <= 4000,
            'INVALID_REQUEST', 'confirmed_brief: must be non-empty text string up to 4000 characters')

    keep = req.get('keep', [])
    change = req.get('change', [])
    require(isinstance(keep, list) and all(isinstance(x, str) and bool(x.strip()) for x in keep),
            'INVALID_REQUEST', 'keep must be a list of non-empty strings')
    require(isinstance(change, list) and all(isinstance(x, str) and bool(x.strip()) for x in change),
            'INVALID_REQUEST', 'change must be a list of non-empty strings')

    sess_id = req.get('session_id')
    if not sess_id:
        sess_id = f'sess_{src_hash[:8]}_{int(time.time())}'

    # Ambiguity check: duplicate exposure_ev in both top-level and adjustments
    has_top_ev = 'exposure_ev' in req
    req_adj = req.get('adjustments')
    has_adj = isinstance(req_adj, dict)
    if has_top_ev and has_adj and 'exposure_ev' in req_adj:
        raise ToolError('INVALID_REQUEST', 'Duplicate exposure_ev specified in both top-level and adjustments')

    has_init_adj = has_adj or has_top_ev
    if has_init_adj:
        try:
            rt = engine()
            engine_ver = rt['version']
        except ToolError:
            engine_ver = '5.13'
        initial_adj = resolve_effective_adjustments(
            {}, req, engine_ver,
            source_type=src_info['source_type'],
            source_path=source
        )
    else:
        initial_adj = {}

    now = datetime.now(timezone.utc).isoformat()
    session_data = {
        'schema_version': 1,
        'session_id': sess_id,
        'created_at': now,
        'updated_at': now,
        'source': {
            'path': str(source),
            'sha256': src_hash,
            'source_type': src_info['source_type']
        },
        'base_profile': {
            'path': prof_meta['path'],
            'sha256': prof_hash
        },
        'confirmed_brief': brief,
        'keep': keep,
        'change': change,
        'revision': 0,
        'effective_adjustments': initial_adj,
        'history': []
    }

    lock_path = Path(str(target) + '.lock')
    with FileLock(lock_path):
        require(not target.exists(), 'SESSION_EXISTS',
                f'Session file already exists: {target}; init refuses to overwrite')
        atomic_write_json(target, session_data)

    return {'session_path': str(target), 'session_id': sess_id, 'revision': 0, 'session': session_data}

def session_load(req):
    require('reset_adjustments' not in req and 'reset_fields' not in req,
            'INVALID_REQUEST', 'reset parameters are not supported in session load')
    target = Path(req['session_path']).expanduser().resolve()
    require(target.is_file(), 'FILE_NOT_FOUND', f'Session file not found: {target}')
    lock_path = Path(str(target) + '.lock')
    with FileLock(lock_path):
        try:
            raw = target.read_text(encoding='utf-8')
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToolError('CORRUPT_SESSION', f'Session file contains invalid JSON: {e}')
        except Exception as e:
            raise ToolError('CORRUPT_SESSION', f'Cannot read session file: {e}')

        require(isinstance(data, dict), 'CORRUPT_SESSION', 'Session file content must be a JSON object')
        ver = data.get('schema_version')
        require(ver == 1, 'UNSUPPORTED_SESSION_SCHEMA',
                f'Unsupported session schema_version: {ver}; expected 1. Do not auto-reset.')
        check_session_resource_anchors(data, target)
        return {
            'session_path': str(target),
            'session_id': data['session_id'],
            'revision': data['revision'],
            'source': data['source'],
            'base_profile': data['base_profile'],
            'confirmed_brief': data['confirmed_brief'],
            'keep': data['keep'],
            'change': data['change'],
            'effective_adjustments': data['effective_adjustments'],
            'history_count': len(data.get('history', [])),
            'session': data
        }

def session_record_feedback(req):
    target = Path(req['session_path']).expanduser().resolve()
    require(target.is_file(), 'FILE_NOT_FOUND', f'Session file not found: {target}')
    require('expected_revision' in req, 'INVALID_REQUEST', 'expected_revision is required for record_feedback')
    exp_rev = req['expected_revision']
    require(type(exp_rev) is int and not isinstance(exp_rev, bool) and exp_rev >= 0,
            'INVALID_REQUEST', 'expected_revision must be non-negative integer')

    new_keep = req.get('keep')
    if new_keep is not None:
        require(isinstance(new_keep, list) and all(isinstance(x, str) and bool(x.strip()) for x in new_keep),
                'INVALID_REQUEST', 'keep must be a list of non-empty strings')

    new_change = req.get('change')
    if new_change is not None:
        require(isinstance(new_change, list) and all(isinstance(x, str) and bool(x.strip()) for x in new_change),
                'INVALID_REQUEST', 'change must be a list of non-empty strings')

    user_feedback = req.get('user_feedback')
    if user_feedback is not None:
        require(isinstance(user_feedback, str) and bool(user_feedback.strip()) and len(user_feedback) <= 4000,
                'INVALID_REQUEST', 'user_feedback must be non-empty string up to 4000 characters')

    new_brief = req.get('confirmed_brief')
    if new_brief is not None:
        require(isinstance(new_brief, str) and bool(new_brief.strip()) and len(new_brief) <= 4000,
                'INVALID_REQUEST', 'confirmed_brief: must be non-empty string up to 4000 characters')

    try:
        rt = engine()
        engine_ver = rt['version']
    except ToolError:
        engine_ver = '5.13'

    has_adj_update = any(key in req for key in ('adjustments', 'exposure_ev', 'reset_adjustments', 'reset_fields'))

    lock_path = Path(str(target) + '.lock')
    with FileLock(lock_path):
        try:
            raw = target.read_text(encoding='utf-8')
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToolError('CORRUPT_SESSION', f'Session file contains invalid JSON: {e}')
        except Exception as e:
            raise ToolError('CORRUPT_SESSION', f'Cannot read session file: {e}')

        require(isinstance(data, dict), 'CORRUPT_SESSION', 'Session file content must be a JSON object')
        ver = data.get('schema_version')
        require(ver == 1, 'UNSUPPORTED_SESSION_SCHEMA',
                f'Unsupported session schema_version: {ver}; expected 1. Do not auto-reset.')
        check_session_resource_anchors(data, target)

        current_rev = data.get('revision', 0)
        require(current_rev == exp_rev, 'REVISION_CONFLICT',
                f'Session revision conflict: expected {exp_rev}, but file is at {current_rev}. Reload before updating.')

        if has_adj_update:
            base_adj = data.get('effective_adjustments', {})
            sess_src_type = data.get('source', {}).get('source_type')
            sess_src_path = data.get('source', {}).get('path')
            resolved_adj = resolve_effective_adjustments(
                base_adj, req, engine_ver,
                source_type=sess_src_type,
                source_path=sess_src_path
            )
        else:
            resolved_adj = None

        now = datetime.now(timezone.utc).isoformat()

        # Archive historical snapshot of the revision being completed
        snapshot = {
            'revision': current_rev,
            'timestamp': data.get('updated_at', now),
            'keep': list(data.get('keep', [])),
            'change': list(data.get('change', [])),
            'confirmed_brief': data.get('confirmed_brief'),
            'effective_adjustments': dict(data.get('effective_adjustments', {})),
            'user_feedback': user_feedback
        }
        data.setdefault('history', []).append(snapshot)

        # Update fields: full replacement if provided, but NEVER clear untouched fields if omitted
        if new_keep is not None:
            data['keep'] = list(new_keep)
        if new_change is not None:
            data['change'] = list(new_change)
        if new_brief is not None:
            data['confirmed_brief'] = new_brief
        if has_adj_update:
            data['effective_adjustments'] = resolved_adj

        data['revision'] = current_rev + 1
        data['updated_at'] = now

        atomic_write_json(target, data)

    return {
        'session_path': str(target),
        'session_id': data['session_id'],
        'revision': data['revision'],
        'keep': data['keep'],
        'change': data['change'],
        'confirmed_brief': data['confirmed_brief'],
        'effective_adjustments': data['effective_adjustments']
    }

def is_candidate_dir_containing_protected_path(candidate_dir, protected_path):
    """Shared helper to determine if candidate_dir contains or is identical to protected_path.
    1. Fast canonical Path hierarchy check.
    2. Physical filesystem identity check (os.path.samefile / st_dev + st_ino)
       covering APFS case-folding aliases and symlinks.
    Fail closed: returns True if verification fails unexpectedly.
    """
    try:
        c = Path(candidate_dir).expanduser().resolve()
        p = Path(protected_path).expanduser().resolve()
        # 1. Fast canonical path hierarchy check
        if c in (p, *p.parents):
            return True
        # 2. Physical filesystem identity check
        if not c.exists():
            return False
        curr = p
        while True:
            try:
                if curr.exists() and os.path.samefile(c, curr):
                    return True
            except OSError:
                pass
            if curr.parent == curr:
                break
            curr = curr.parent
        return False
    except Exception:
        # Fail closed: treat as protected to prevent accidental deletion
        return True

def prune_history(req):
    """Remove only selected committed raster files; retain the provenance container."""
    target = file_path(req['session_path'])
    require(not Path(req['session_path']).expanduser().is_symlink(), 'SECURITY_VIOLATION', 'Session cannot be a symlink')
    with ExitStack() as locks:
        gate = FileLock(Path(str(target) + '.render.gate'))
        require(gate.acquire(non_blocking=True), 'RENDER_IN_PROGRESS', 'Session is rendering; do not prune')
        locks.callback(gate.release)
        guard_paths = [file_path(p) for p in req.get('protect_session_paths', [])]
        identities = [(p.stat().st_dev, p.stat().st_ino) for p in [target, *guard_paths]]
        require(len(set(identities)) == len(identities), 'INVALID_REQUEST',
                'Protection sessions must be physically unique and different from the target')
        for path in sorted([target, *guard_paths]):
            locks.enter_context(FileLock(Path(str(path) + '.lock')))
        try:
            data = json.loads(target.read_text(encoding='utf-8'))
        except Exception as exc:
            raise ToolError('CORRUPT_SESSION', f'Cannot read session: {exc}') from exc
        require(isinstance(data, dict) and type(data.get('schema_version')) is int and data['schema_version'] == 1,
                'CORRUPT_SESSION', 'Unsupported or invalid session')
        require(data.get('revision') == req['expected_revision'], 'REVISION_CONFLICT', 'Session revision changed; reload before pruning')
        check_session_resource_anchors(data, target)
        history = data.get('history', [])
        require(isinstance(history, list) and all(isinstance(h, dict) for h in history), 'CORRUPT_SESSION', 'Invalid history')
        renders = [h for h in history if h.get('status') == 'SUCCESS']
        require(bool(renders), 'INVALID_REQUEST', 'No committed renders to prune')
        requested = [str(Path(p).expanduser().resolve()) for p in req['manifest_paths']]
        require(len(set(requested)) == len(requested), 'INVALID_REQUEST', 'Duplicate canonical manifest paths')
        known = {str(Path(h['manifest_path']).resolve()): h for h in renders if h.get('manifest_path')}
        require(len(known) == len(renders), 'CORRUPT_SESSION', 'Duplicate or missing history manifest')
        require(set(requested) <= set(known), 'INVALID_REQUEST', 'Only committed history manifests can be selected')
        latest = str(Path(renders[-1]['manifest_path']).resolve())
        require(latest not in requested, 'PROTECTED_ARTIFACT', 'The latest committed render is always retained')
        protected = [target, Path(str(target) + '.lock'), Path(str(target) + '.render.gate')]
        for key in ('source', 'base_profile'):
            value = data.get(key, {}).get('path')
            require(isinstance(value, str) and bool(value), 'CORRUPT_SESSION', f'Missing {key} path')
            protected.append(Path(value).resolve())
        for adj in [data.get('effective_adjustments', {}), *[h.get('effective_adjustments', h.get('adjustments', {})) for h in history]]:
            if isinstance(adj, dict):
                for key in ('lens_correction', 'input_profile'):
                    resource = adj.get(key, {})
                    if isinstance(resource, dict) and resource.get('path'):
                        protected.append(Path(resource['path']).expanduser().resolve())
        for manifest, h in known.items():
            if manifest not in requested:
                protected.extend(Path(h[key]).resolve() for key in ('output_path', 'preview_path', 'manifest_path') if h.get(key))
        protected.extend(session_output_profile_paths(data))
        for guard_path in guard_paths:
            try:
                guard_data = json.loads(guard_path.read_text(encoding='utf-8'))
                require(isinstance(guard_data, dict) and type(guard_data.get('schema_version')) is int and guard_data['schema_version'] == 1,
                        'CORRUPT_SESSION', 'Invalid protection session')
                check_session_resource_anchors(guard_data, guard_path)
                protected.append(guard_path)
                for key in ('source', 'base_profile'):
                    value = guard_data.get(key, {}).get('path')
                    require(isinstance(value, str) and Path(value).is_absolute(), 'CORRUPT_SESSION', 'Invalid protection session binding')
                    protected.append(Path(value).resolve())
                protected.extend(session_output_profile_paths(guard_data))
                for h in guard_data.get('history', []):
                    for key in ('output_path', 'preview_path', 'manifest_path'):
                        if h.get(key):
                            protected.append(Path(h[key]).resolve())
                for adj in [guard_data.get('effective_adjustments', {}), *[h.get('effective_adjustments', h.get('adjustments', {})) for h in guard_data.get('history', [])]]:
                    for key in ('lens_correction', 'input_profile'):
                        resource = adj.get(key, {})
                        if resource.get('path'):
                            protected.append(Path(resource['path']).resolve())
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError('CORRUPT_SESSION', f'Cannot load protection session {guard_path}: {exc}') from exc
        candidates = []
        for manifest_name in requested:
            h = known[manifest_name]
            raw_manifest = Path(h['manifest_path'])
            require(raw_manifest.is_absolute() and not raw_manifest.is_symlink() and not raw_manifest.parent.is_symlink(), 'SECURITY_VIOLATION', 'Render container cannot be a symlink')
            manifest = file_path(manifest_name)
            require(manifest.name == 'manifest.json' and digest(manifest) == h.get('manifest_sha256'),
                    'CHECKSUM_MISMATCH', 'History manifest changed')
            m = json.loads(manifest.read_text(encoding='utf-8'))
            require(m.get('session_id') == data.get('session_id') and m.get('source_sha256') == data['source']['sha256']
                    and m.get('profile', {}).get('sha256') == data['base_profile']['sha256'],
                    'SESSION_IDENTITY_CHANGED', 'Manifest does not belong to this session binding')
            active = FileLock(manifest.parent / '.active.lock')
            require((manifest.parent / '.active.lock').is_file() and not (manifest.parent / '.active.lock').is_symlink(),
                    'SECURITY_VIOLATION', 'Missing valid render lock')
            require(active.acquire(non_blocking=True), 'RENDER_IN_PROGRESS', 'Selected artifact is active')
            locks.callback(active.release)
            files = []
            for key, hash_key, names in [('output_path', 'output_sha256', {'image.jpg', 'image.tif'}),
                                         ('preview_path', 'preview_sha256', {'preview.jpg'})]:
                if not h.get(key):
                    continue
                raw = Path(h[key]); path = raw.expanduser().resolve()
                require(raw.is_absolute() and not raw.is_symlink() and path.parent == manifest.parent and path.name in names,
                        'SECURITY_VIOLATION', 'Raster is outside its render container or has an unexpected name')
                expected = h.get(hash_key)
                manifest_hash = m.get('output_sha256') if key == 'output_path' else m.get('preview', {}).get('sha256')
                require(isinstance(expected, str) and expected == manifest_hash, 'CHECKSUM_MISMATCH', 'Raster history and manifest differ')
                require(not any(path == q or (path.exists() and q.exists() and os.path.samefile(path, q)) for q in protected),
                        'PROTECTED_ARTIFACT', f'Raster is a protected source, resource or retained artifact: {path}')
                if not path.exists():
                    require(h.get('artifact_state') in {'prune_pending', 'prune_partial', 'pruned'}, 'FILE_NOT_FOUND', f'Raster unexpectedly missing: {path}')
                    continue
                require(path.is_file() and digest(path) == expected, 'CHECKSUM_MISMATCH', f'Raster changed: {path}')
                files.append({'path': str(path), 'sha256': expected, 'logical_bytes': path.stat().st_size})
            candidates.append({'manifest': manifest_name, 'files': files})
        result = {'apply': req.get('apply', False), 'session_path': str(target), 'session_revision': data['revision'],
                  'candidates': candidates, 'reclaimable_logical_bytes': sum(f['logical_bytes'] for c in candidates for f in c['files']),
                  'deleted_files': [], 'errors': [], 'status': 'preview', 'retained_latest_manifest': latest}
        if not req.get('apply', False):
            return result
        pending = [c for c in candidates if c['files'] or known[c['manifest']].get('artifact_state') != 'pruned']
        if not pending:
            result['status'] = 'no_op'
            return result
        for candidate in pending:
            known[candidate['manifest']]['artifact_state'] = 'prune_pending'
        data['revision'] += 1
        data['updated_at'] = datetime.now(timezone.utc).isoformat()
        try:
            atomic_write_json(target, data)
        except Exception as exc:
            raise ToolError('SESSION_COMMIT_FAILED', f'Could not record prune intent; no files deleted: {exc}') from exc
        for candidate in pending:
            failed = False
            for info in candidate['files']:
                path = Path(info['path'])
                try:
                    require(not path.is_symlink() and path.is_file() and digest(path) == info['sha256'],
                            'CHECKSUM_MISMATCH', f'Raster changed before deletion: {path}')
                    path.unlink()
                    result['deleted_files'].append(info)
                except (OSError, ToolError) as exc:
                    failed = True
                    result['errors'].append({'path': str(path), 'message': str(exc)})
            known[candidate['manifest']]['artifact_state'] = 'prune_partial' if failed else 'pruned'
            known[candidate['manifest']]['artifact_pruned_at'] = data['updated_at']
        try:
            atomic_write_json(target, data)
        except Exception as exc:
            raise ToolError('SESSION_COMMIT_FAILED', f'Prune intent was saved, but final status could not be saved: {exc}',
                            {'session_path': str(target), 'pending_revision': data['revision'], 'deleted_files': result['deleted_files']}) from exc
        result.update(session_revision=data['revision'], status='partial' if result['errors'] else 'complete')
        return result

def clean(req):
    require('output_dir' in req, 'INVALID_REQUEST', 'output_dir is required for clean')

    # 1. Mutual exclusion between session_path and session_paths
    has_sp = 'session_path' in req
    has_sps = 'session_paths' in req
    if (has_sp and has_sps) or (not has_sp and not has_sps):
        raise ToolError('INVALID_REQUEST', 'Exactly one of session_path or session_paths must be provided')

    if has_sp:
        sp_val = req['session_path']
        require(isinstance(sp_val, str) and bool(sp_val.strip()), 'INVALID_REQUEST', 'session_path must be a non-empty string')
        raw_paths = [sp_val]
    else:
        raw_paths = req['session_paths']
        require(isinstance(raw_paths, list) and not isinstance(raw_paths, (str, bytes)),
                'INVALID_REQUEST', 'session_paths must be an array')
        require(len(raw_paths) > 0, 'INVALID_REQUEST', 'session_paths cannot be empty')
        for p in raw_paths:
            require(isinstance(p, str) and bool(p.strip()), 'INVALID_REQUEST', 'session_paths items must be non-empty strings')

    output_dir_raw = Path(req['output_dir'])
    require(not output_dir_raw.is_symlink(), 'SECURITY_VIOLATION', 'output_dir cannot be a symlink')
    output_dir = output_dir_raw.expanduser().resolve()
    require(not output_dir.is_symlink(), 'SECURITY_VIOLATION', 'output_dir cannot be a symlink')
    require(output_dir.is_dir(), 'FILE_NOT_FOUND', f'output_dir not found or not a directory: {output_dir}')
    require(output_dir != Path('/') and output_dir != Path.home(), 'SECURITY_VIOLATION',
            'Refusing to run clean on root or home directory')

    apply = req.get('apply', False)
    require(isinstance(apply, bool), 'INVALID_REQUEST', 'apply must be a boolean')

    min_age = req.get('min_age_seconds', 1800)
    require(type(min_age) is int and not isinstance(min_age, bool) and min_age >= 0,
            'INVALID_REQUEST', 'min_age_seconds must be a non-negative integer')

    preflight = req.get('preflight_only', False)
    require(isinstance(preflight, bool), 'INVALID_REQUEST', 'preflight_only must be a boolean')
    require(not (preflight and apply), 'INVALID_REQUEST', 'preflight_only cannot be combined with apply=true')
    if preflight:
        # Inspect every session independently, but never delete with incomplete protection data.
        normalized_paths = [str(Path(p).expanduser().resolve()) for p in raw_paths]
        require(len(normalized_paths) == len(set(normalized_paths)), 'INVALID_REQUEST',
                'Duplicate session paths in preflight request')
        reports = []
        preflight_session_ids = {}
        for path in raw_paths:
            try:
                result = clean({'action': 'clean', 'session_path': path,
                                'output_dir': str(output_dir), 'apply': False,
                                'min_age_seconds': min_age})
                session_id = result['session_id']
                require(session_id not in preflight_session_ids, 'INVALID_REQUEST',
                        f'Duplicate session_id {session_id} in {path} and {preflight_session_ids.get(session_id)}')
                preflight_session_ids[session_id] = path
                reports.append({'session_path': path, 'ok': True,
                                'session_id': session_id,
                                'orphan_count': result['total_orphans'],
                                'reclaimable_bytes': result['total_bytes']})
            except ToolError as exc:
                reports.append({'session_path': path, 'ok': False,
                                'error': {'code': exc.code, 'message': str(exc)}})
            except Exception as exc:
                reports.append({'session_path': path, 'ok': False,
                                'error': {'code': 'PREFLIGHT_FAILED',
                                          'message': f'Preflight could not complete: {type(exc).__name__}: {exc}'}})
        succeeded = sum(r['ok'] for r in reports)
        return {'preflight_only': True, 'apply': False, 'deleted_count': 0,
                'status': 'complete' if succeeded == len(reports) else ('partial' if succeeded else 'failed'),
                'sessions': reports, 'succeeded': succeeded, 'failed': len(reports) - succeeded,
                'normal_clean_precheck_passed': succeeded == len(reports),
                'note': 'Read-only per-session diagnostics. Candidates are provisional; normal clean rechecks all sessions before deletion.'}

    # 2. Canonicalize & Deduplicate by normalized real path
    canonical_sessions = []
    seen_paths = set()
    for p_str in raw_paths:
        p_raw = Path(p_str)
        require(not p_raw.is_symlink(), 'SECURITY_VIOLATION', f'session path cannot be a symlink: {p_str}')
        p_canon = p_raw.expanduser().resolve()
        require(not p_canon.is_symlink(), 'SECURITY_VIOLATION', f'session path cannot be a symlink: {p_canon}')
        require(p_canon.is_file(), 'FILE_NOT_FOUND', f'Session file not found: {p_canon}')
        if p_canon in seen_paths:
            raise ToolError('INVALID_REQUEST', f'Duplicate session path specified in session_paths: {p_str}')
        seen_paths.add(p_canon)
        canonical_sessions.append(p_canon)

    # 3. Fail-closed Pre-check of ALL sessions & duplicate session_id check
    sessions_meta = []
    session_id_to_path = {}
    for sess_path in canonical_sessions:
        sess_lock_path = Path(str(sess_path) + '.lock')
        with FileLock(sess_lock_path):
            try:
                raw = sess_path.read_text(encoding='utf-8')
                s_data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ToolError('CORRUPT_SESSION', f'Session file contains invalid JSON: {e}')
            except Exception as e:
                raise ToolError('CORRUPT_SESSION', f'Cannot read session file: {e}')

            require(isinstance(s_data, dict), 'CORRUPT_SESSION', 'Session file content must be a JSON object')
            ver = s_data.get('schema_version')
            require(type(ver) is int and not isinstance(ver, bool) and ver == 1,
                    'UNSUPPORTED_SESSION_SCHEMA', f'Unsupported session schema_version: {ver}')
            check_session_resource_anchors(s_data, sess_path)
            sess_id = s_data.get('session_id')
            require(isinstance(sess_id, str) and bool(sess_id.strip()),
                    'CORRUPT_SESSION', 'Session file missing valid session_id')

            # Ambiguity check: duplicate session_id across distinct files
            if sess_id in session_id_to_path and session_id_to_path[sess_id] != sess_path:
                raise ToolError('INVALID_REQUEST',
                                f"Duplicate session_id '{sess_id}' found in distinct session files: {sess_path} and {session_id_to_path[sess_id]}")
            session_id_to_path[sess_id] = sess_path

            src_hash = s_data.get('source', {}).get('sha256')
            prof_hash = s_data.get('base_profile', {}).get('sha256')
            src_path_str = s_data.get('source', {}).get('path', '')
            prof_path_str = s_data.get('base_profile', {}).get('path', '')
            require(bool(src_hash) and bool(prof_hash) and bool(src_path_str) and bool(prof_path_str),
                    'CORRUPT_SESSION', 'Session file missing source or base_profile metadata')
            src_path = Path(src_path_str).expanduser().resolve()
            prof_path = Path(prof_path_str).expanduser().resolve()

            resource_paths = []
            eff_adj = s_data.get('effective_adjustments', {})
            if isinstance(eff_adj, dict):
                lc = eff_adj.get('lens_correction')
                if isinstance(lc, dict) and lc.get('path'):
                    resource_paths.append(Path(lc['path']).expanduser().resolve())
                ip = eff_adj.get('input_profile')
                if isinstance(ip, dict) and ip.get('path'):
                    resource_paths.append(Path(ip['path']).expanduser().resolve())
            for h in s_data.get('history', []):
                h_adj = h.get('effective_adjustments', h.get('adjustments', {}))
                if isinstance(h_adj, dict):
                    h_lc = h_adj.get('lens_correction')
                    if isinstance(h_lc, dict) and h_lc.get('path'):
                        resource_paths.append(Path(h_lc['path']).expanduser().resolve())
                    h_ip = h_adj.get('input_profile')
                    if isinstance(h_ip, dict) and h_ip.get('path'):
                        resource_paths.append(Path(h_ip['path']).expanduser().resolve())
            resource_paths.extend(session_output_profile_paths(s_data))
            resource_paths = list(dict.fromkeys(resource_paths))

            sessions_meta.append({
                'path': sess_path,
                'lock_path': sess_lock_path,
                'session_id': sess_id,
                'source_sha256': src_hash,
                'profile_sha256': prof_hash,
                'source_path': src_path,
                'profile_path': prof_path,
                'resource_paths': resource_paths,
            })

    skipped = []
    candidates = []

    try:
        entries = sorted(output_dir.iterdir(), key=lambda p: p.name)
    except Exception as e:
        raise ToolError('IO_ERROR', f'Cannot list output_dir: {e}')

    now = time.time()
    for item in entries:
        if item.is_symlink():
            skipped.append({'path': str(item), 'reason': 'symlink_entry_rejected'})
            continue
        if not item.is_dir():
            skipped.append({'path': str(item), 'reason': 'not_a_directory'})
            continue
        try:
            item.resolve().relative_to(output_dir)
        except ValueError:
            skipped.append({'path': str(item), 'reason': 'path_traversal_escape'})
            continue

        item_canon = item.resolve()

        # Protected files check across all target sessions:
        # Candidate directory (item) must not contain (be an ancestor of or equal to) any protected file
        is_protected_file = False
        for sm in sessions_meta:
            protected_paths = [
                sm['path'],
                sm['lock_path'],
                sm['source_path'],
                sm['profile_path'],
                *sm.get('resource_paths', [])
            ]
            if any(is_candidate_dir_containing_protected_path(item, p) for p in protected_paths):
                is_protected_file = True
                break
        if is_protected_file:
            skipped.append({'path': str(item), 'reason': 'protected_user_file_inside'})
            continue

        manifest_path = item / 'manifest.json'
        if manifest_path.is_symlink():
            skipped.append({'path': str(item), 'reason': 'manifest_is_symlink'})
            continue
        if not manifest_path.is_file():
            skipped.append({'path': str(item), 'reason': 'missing_manifest'})
            continue

        try:
            m_data = json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception:
            skipped.append({'path': str(item), 'reason': 'corrupt_manifest'})
            continue

        if not isinstance(m_data, dict):
            skipped.append({'path': str(item), 'reason': 'invalid_manifest_content'})
            continue

        # Check schema_version: strictly int 1 (not bool)
        m_schema_ver = m_data.get('schema_version')
        if type(m_schema_ver) is not int or isinstance(m_schema_ver, bool) or m_schema_ver != 1:
            skipped.append({'path': str(item), 'reason': 'unsupported_manifest_schema'})
            continue

        # Check cleanup_protocol_version
        if 'cleanup_protocol_version' in m_data:
            m_cleanup_ver = m_data['cleanup_protocol_version']
            if type(m_cleanup_ver) is not int or isinstance(m_cleanup_ver, bool) or m_cleanup_ver != 1:
                skipped.append({'path': str(item), 'reason': f'unsupported_cleanup_protocol ({m_cleanup_ver})'})
                continue
            # Explicit cleanup_protocol_version 1 satisfied!
        else:
            # Missing cleanup_protocol_version: only known 0.4.1 legacy branch allowed!
            # Explicit unknown cannot fall back!
            m_tool_ver = m_data.get('tool_version')
            if m_tool_ver != '0.4.1':
                skipped.append({'path': str(item), 'reason': f'legacy_or_unknown_tool_version ({m_tool_ver})'})
                continue

        m_sess_id = m_data.get('session_id')
        if not m_sess_id:
            skipped.append({'path': str(item), 'reason': 'stateless_unbound_render'})
            continue

        target_meta = next((sm for sm in sessions_meta if sm['session_id'] == m_sess_id), None)
        if target_meta is None:
            skipped.append({'path': str(item), 'reason': f'other_session_render ({m_sess_id})'})
            continue

        if m_data.get('source_sha256') != target_meta['source_sha256'] or \
           m_data.get('profile', {}).get('sha256') != target_meta['profile_sha256']:
            skipped.append({'path': str(item), 'reason': 'session_identity_mismatch'})
            continue

        lock_file = item / '.active.lock'
        if not lock_file.is_file() or lock_file.is_symlink():
            skipped.append({'path': str(item), 'reason': 'missing_or_symlink_active_lock_file'})
            continue

        prod_lock = FileLock(lock_file)
        if not prod_lock.acquire(non_blocking=True):
            skipped.append({'path': str(item), 'reason': 'active_render_in_progress'})
            continue

        try:
            mtime = max(manifest_path.stat().st_mtime, item.stat().st_mtime)
            age = now - mtime
            if age < min_age:
                skipped.append({'path': str(item), 'reason': f'too_young (age={int(age)}s < {min_age}s)'})
                continue

            # Fresh read session history across ALL specified sessions right before deletion
            is_committed = False
            is_protected_fresh = False
            for sm in sessions_meta:
                with FileLock(sm['lock_path']):
                    try:
                        reloaded_sess = json.loads(sm['path'].read_text(encoding='utf-8'))
                    except Exception as e:
                        raise ToolError('CORRUPT_SESSION', f'Cannot re-read session {sm["path"]}: {e}')

                    require(isinstance(reloaded_sess, dict), 'CORRUPT_SESSION', 'Session file content must be a JSON object')
                    ver = reloaded_sess.get('schema_version')
                    require(type(ver) is int and not isinstance(ver, bool) and ver == 1,
                            'UNSUPPORTED_SESSION_SCHEMA', f'Unsupported session schema_version in {sm["path"]}: {ver}')
                    check_session_resource_anchors(reloaded_sess, sm['path'])
                    require(reloaded_sess.get('session_id') == sm['session_id'], 'SESSION_ID_MISMATCH',
                            f'Session ID changed from {sm["session_id"]} to {reloaded_sess.get("session_id")}')
                    require(reloaded_sess.get('source', {}).get('sha256') == sm['source_sha256'], 'SESSION_IDENTITY_CHANGED',
                            f'Session source changed in {sm["path"]}')
                    require(reloaded_sess.get('base_profile', {}).get('sha256') == sm['profile_sha256'], 'SESSION_IDENTITY_CHANGED',
                            f'Session base_profile changed in {sm["path"]}')

                    fresh_resources = []
                    eff_adj = reloaded_sess.get('effective_adjustments', {})
                    if isinstance(eff_adj, dict):
                        lc = eff_adj.get('lens_correction')
                        if isinstance(lc, dict) and lc.get('path'):
                            fresh_resources.append(Path(lc['path']).expanduser().resolve())
                        ip = eff_adj.get('input_profile')
                        if isinstance(ip, dict) and ip.get('path'):
                            fresh_resources.append(Path(ip['path']).expanduser().resolve())
                    for h in reloaded_sess.get('history', []):
                        h_adj = h.get('effective_adjustments', h.get('adjustments', {}))
                        if isinstance(h_adj, dict):
                            h_lc = h_adj.get('lens_correction')
                            if isinstance(h_lc, dict) and h_lc.get('path'):
                                fresh_resources.append(Path(h_lc['path']).expanduser().resolve())
                            h_ip = h_adj.get('input_profile')
                            if isinstance(h_ip, dict) and h_ip.get('path'):
                                fresh_resources.append(Path(h_ip['path']).expanduser().resolve())
                        if h.get('status') == 'SUCCESS':
                            h_out_str = h.get('output_path')
                            h_man_str = h.get('manifest_path')
                            if h_out_str and is_candidate_dir_containing_protected_path(item, h_out_str):
                                is_committed = True
                            if h_man_str and is_candidate_dir_containing_protected_path(item, h_man_str):
                                is_committed = True

                    fresh_resources.extend(session_output_profile_paths(reloaded_sess))
                    fresh_protected = [
                        sm['path'],
                        sm['lock_path'],
                        Path(reloaded_sess.get('source', {}).get('path', '')).expanduser().resolve(),
                        Path(reloaded_sess.get('base_profile', {}).get('path', '')).expanduser().resolve(),
                        *fresh_resources
                    ]
                    if any(is_candidate_dir_containing_protected_path(item, p) for p in fresh_protected):
                        is_protected_fresh = True
                        break
                    if is_committed:
                        break

            if is_protected_fresh:
                skipped.append({'path': str(item), 'reason': 'protected_user_file_inside'})
                continue

            if is_committed:
                skipped.append({'path': str(item), 'reason': 'committed_to_session_history'})
                continue

            has_symlink_inside = False
            total_bytes = 0
            for root, dirs, files in os.walk(item, followlinks=False):
                for d in dirs:
                    dp = Path(root) / d
                    if dp.is_symlink():
                        has_symlink_inside = True
                        break
                for f in files:
                    fp = Path(root) / f
                    if fp.is_symlink():
                        has_symlink_inside = True
                        break
                    try:
                        total_bytes += fp.stat().st_size
                    except OSError:
                        pass
                if has_symlink_inside:
                    break

            if has_symlink_inside:
                skipped.append({'path': str(item), 'reason': 'contains_internal_symlink'})
                continue

            if apply:
                shutil.rmtree(item)
                candidates.append({
                    'path': str(item),
                    'session_id': m_sess_id,
                    'reclaimed_bytes': total_bytes,
                    'age_seconds': int(age)
                })
            else:
                candidates.append({
                    'path': str(item),
                    'session_id': m_sess_id,
                    'reclaimable_bytes': total_bytes,
                    'age_seconds': int(age)
                })
        finally:
            prod_lock.release()

    res = {
        'output_dir': str(output_dir),
        'apply': apply,
        'min_age_seconds': min_age,
        'orphans': candidates,
        'total_orphans': len(candidates),
        'total_bytes': sum(c.get('reclaimed_bytes', c.get('reclaimable_bytes', 0)) for c in candidates),
        'skipped': skipped
    }
    if has_sp:
        res['session_path'] = str(canonical_sessions[0])
        res['session_id'] = sessions_meta[0]['session_id']
    else:
        res['session_paths'] = [str(s) for s in canonical_sessions]
        res['session_ids'] = [sm['session_id'] for sm in sessions_meta]

    return res

def dispatch(req):
    validate(req)
    action = req['action']
    if action == 'clean':
        return clean(req)
    if action == 'doctor':
        try:
            rt = engine()
        except ToolError as e:
            rt = {'error': e.code, 'message': str(e)}

        rawpy_available = False
        libraw_ver = None
        if importlib.util.find_spec('rawpy') is not None:
            try:
                import rawpy
                rawpy_available = True
                libraw_ver = getattr(rawpy, 'libraw_version', None)
            except Exception:
                rawpy_available = False
        numpy_available = importlib.util.find_spec('numpy') is not None
        raw_qc_available = rawpy_available and numpy_available

        raw_qc_hint = None
        if not raw_qc_available:
            missing = []
            if not rawpy_available:
                missing.append('rawpy')
            if not numpy_available:
                missing.append('numpy')
            raw_qc_hint = f"Optional CFA diagnostic action 'raw_qc' requires: {' and '.join(missing)}. Install in your environment with: pip install {' '.join(missing)} (or uv run --with {' --with '.join(missing)} ...)"

        doc_res = {
            'tool_version': VERSION,
            'architecture': platform.machine(),
            'engine': rt,
            'exiftool': shutil.which('exiftool'),
            'exiftool_install_hint': None if shutil.which('exiftool') else exiftool_hint(),
            'pillow': importlib.util.find_spec('PIL') is not None,
            'rawpy': rawpy_available,
            'numpy': numpy_available,
            'raw_qc_available': raw_qc_available,
            'raw_qc_install_hint': raw_qc_hint
        }
        if libraw_ver:
            doc_res['libraw_version'] = libraw_ver
        return doc_res
    if action == 'capabilities':
        return capabilities(req)
    if action == 'profile':
        kind = req.get('kind', 'pp3')
        return profile(req['profile'], kind=kind)[0]
    if action == 'inspect':
        p = file_path(req['source'])
        src_info = check_source(p)
        exif = shutil.which('exiftool')
        require(exif, 'DEPENDENCY_MISSING', exiftool_hint())
        res = {'source': str(p), 'source_type': src_info['source_type'], 'sha256': digest(p),
               'metadata': json.loads(run([exif, '-j', '-n', str(p)], stdout_only=True))}
        if req.get('lens_check', False):
            res['lens_support'] = lens_support_check(res['metadata'])
            res['lens_support_hint'] = res['lens_support']['hint']
        if 'container' in src_info:
            res['container'] = src_info['container']
        if 'container_check' in src_info:
            res['container_check'] = src_info['container_check']
        if src_info['source_type'] == 'jpeg':
            res['has_icc'] = src_info.get('has_icc')
            res['icc_description'] = src_info.get('icc_description')
            res['orientation'] = src_info.get('orientation')
        return res
    if action == 'qc':
        return qc(req['image'])
    if action == 'raw_qc':
        return raw_qc(req)
    if action == 'prune_history':
        return prune_history(req)
    if action == 'session':
        op = req['operation']
        if op == 'init':
            return session_init(req)
        if op == 'load':
            return session_load(req)
        if op == 'record_feedback':
            return session_record_feedback(req)
        raise ToolError('INVALID_REQUEST', f'Unknown session operation: {op}')
    return render(req)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--schema', action='store_true')
    group.add_argument('--request', help='UTF-8 JSON file, or - for stdin')
    args = parser.parse_args()
    try:
        if args.schema:
            result = schema()
        else:
            import sys
            raw = sys.stdin.read() if args.request == '-' else Path(args.request).read_text(encoding='utf-8')
            req = json.loads(raw)
            result = {'ok': True, 'result': dispatch(req)}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception as e:
        code = e.code if isinstance(e, ToolError) else ('DEPENDENCY_MISSING' if isinstance(e, ImportError) else 'INVALID_OR_FAILED')
        error = {'code': code, 'message': str(e)}
        if isinstance(e, ToolError) and e.details is not None:
            error['details'] = e.details
        print(json.dumps({'ok': False, 'error': error}, ensure_ascii=False))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())

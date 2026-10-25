# /// script
# dependencies = ["Pillow"]
# ///
"""Real NEF integration smoke. Explicit source/output paths; no mocks or bundled photos."""
import argparse
import io
import json
from pathlib import Path
import tempfile
import time

import rt_tool as rt


def check(condition, message):
    if not condition:
        raise rt.ToolError('SMOKE_ASSERTION_FAILED', message)


def smoke(source, output_dir, timeout_seconds):
    try:
        from PIL import Image, ImageCms
    except ImportError as exc:
        raise rt.ToolError('DEPENDENCY_MISSING', 'Pillow is required; use uv run --script smoke_raw.py with the same arguments') from exc
    source = rt.file_path(source)
    check(source.suffix.lower() == '.nef', 'An actual Nikon NEF is required')
    rt.check_source(source)
    original_hash = rt.digest(source)
    parent = Path(output_dir).expanduser().resolve()
    rt.check_render_space(parent)
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='raw-smoke-', dir=parent))
    report = {'ok': False, 'source': str(source), 'source_sha256': original_hash,
              'workspace': str(root), 'cases': [],
              'verification_scope': 'Real decoder/export/session/artifact integrity; not aesthetic, banding or exhaustive corruption validation.'}
    started = time.monotonic()
    try:
        engine = rt.engine()
        report['engine'] = engine
        check(engine['version'] == '5.13', 'This smoke targets RawTherapee 5.13')
        inspected = rt.dispatch({'action': 'inspect', 'source': str(source)})
        report['metadata'] = inspected['metadata']
        base, _ = rt.profile('builtin:neutral-rec2020-rt513')
        session = root / 'session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(source), 'base_profile': base['path'],
                     'confirmed_brief': 'Real NEF integration smoke using the neutral base; verify exports, not aesthetic acceptance.'})
        dimensions = None
        for revision, fmt in enumerate(('jpeg', 'tiff16')):
            before = time.monotonic()
            result = rt.dispatch({'action': 'render', 'source': str(source),
                                  'profile': base['path'], 'profile_sha256': base['sha256'],
                                  'engine_version': engine['version'], 'output_dir': str(root / 'renders'),
                                  'format': fmt, 'session_path': str(session), 'expected_revision': revision,
                                  'timeout_seconds': timeout_seconds})
            with Image.open(result['output']) as image:
                image.load()
                check(image.info.get('icc_profile'), 'Full output must contain an ICC profile')
                if fmt == 'tiff16':
                    check(tuple(image.tag_v2.get(258, ())) == (16, 16, 16), 'TIFF must declare 16-bit RGB samples')
                else:
                    check(image.format == 'JPEG', 'JPEG output must decode as JPEG')
                size = image.size
            check(dimensions is None or dimensions == size, 'JPEG/TIFF output dimensions disagree')
            dimensions = size
            with Image.open(result['preview']['path']) as preview:
                preview.load()
                check(preview.format == 'JPEG' and max(preview.size) <= 1600, 'Invalid viewing proxy')
                profile = ImageCms.ImageCmsProfile(io.BytesIO(preview.info['icc_profile']))
                check('sRGB' in ImageCms.getProfileDescription(profile), 'Preview must embed sRGB')
            manifest = json.loads(Path(result['manifest']).read_text())
            state = json.loads(session.read_text())
            history = state['history'][-1]
            check(state['revision'] == revision + 1, 'Session revision did not advance once')
            check(history['manifest_sha256'] == rt.digest(result['manifest']), 'Manifest history hash mismatch')
            check(manifest['output_sha256'] == rt.digest(result['output']), 'Full output hash mismatch')
            check(history['preview_sha256'] == rt.digest(result['preview']['path']), 'Preview history hash mismatch')
            check(manifest['source_sha256'] == original_hash, 'Manifest source hash mismatch')
            report['cases'].append({'case': fmt, 'ok': True, 'seconds': round(time.monotonic() - before, 3),
                                    'output_dimensions': list(size), 'output_size_bytes': Path(result['output']).stat().st_size,
                                    'output': result['output'], 'manifest': result['manifest'], 'preview': result['preview']})

        # Keep only a prefix of this real source; never change the source in place.
        damaged = root / 'truncated.NEF'
        with source.open('rb') as reader:
            damaged.write_bytes(reader.read(min(65536, max(4, source.stat().st_size // 4))))
        try:
            rt.dispatch({'action': 'render', 'source': str(damaged), 'profile': base['path'],
                         'profile_sha256': base['sha256'], 'engine_version': engine['version'],
                         'output_dir': str(root / 'corrupt-output'), 'format': 'jpeg', 'timeout_seconds': 30})
        except rt.ToolError as exc:
            check(exc.code in {'PROCESS_FAILED', 'CORRUPT_INPUT', 'UNSUPPORTED_INPUT', 'OUTPUT_INVALID'},
                  f'Unexpected truncated RAW failure: {exc.code}: {exc}')
            report['cases'].append({'case': 'truncated_real_nef', 'ok': True, 'error_code': exc.code,
                                    'error_message': str(exc), 'copy_size_bytes': damaged.stat().st_size})
        else:
            raise rt.ToolError('SMOKE_ASSERTION_FAILED', 'Engine unexpectedly accepted the truncated NEF')
        check(rt.digest(source) == original_hash, 'Original NEF changed')
        check(rt.digest(base['path']) == base['sha256'], 'Base PP3 changed')
        report['source_unchanged'] = True
        report['ok'] = True
    except Exception as exc:
        report['error'] = {'code': exc.code if isinstance(exc, rt.ToolError) else 'SMOKE_FAILED', 'message': str(exc)}
    report['seconds'] = round(time.monotonic() - started, 3)
    report['report_path'] = str(root / 'smoke-report.json')
    rt.atomic_write_json(report['report_path'], report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output-dir', required=True, help='Parent for a new isolated workspace; artifacts are retained')
    parser.add_argument('--timeout-seconds', type=int, default=rt.RENDER_TIMEOUT_RULE['default'])
    args = parser.parse_args()
    try:
        rt.validate_property(args.timeout_seconds, rt.RENDER_TIMEOUT_RULE, 'timeout_seconds')
        report = smoke(args.source, args.output_dir, args.timeout_seconds)
    except Exception as exc:
        report = {'ok': False, 'error': {'code': exc.code if isinstance(exc, rt.ToolError) else 'SMOKE_FAILED', 'message': str(exc)}}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

# /// script
# dependencies = ["Pillow", "numpy", "jsonschema"]
# ///
"""Run with uv run --script scripts/test_rt_tool.py; all image fixtures are synthetic."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
import types

# Fail before running any test; missing dependencies must not become a false green skip.
missing = []
for module, package in [('PIL', 'Pillow'), ('numpy', 'numpy'), ('jsonschema', 'jsonschema')]:
    try:
        importlib.import_module(module)
    except (ImportError, OSError) as exc:
        missing.append(f'{package}: {exc}')
if missing:
    import shlex
    sys.exit('TEST_DEPENDENCY_MISSING (no tests ran): ' + '; '.join(missing) + '\n'
             'Run the full suite in an isolated environment:\n  uv run --script '
             + shlex.quote(str(Path(__file__).resolve())) + '\n'
             'Or install into this Python: ' + shlex.quote(sys.executable)
             + ' -m pip install Pillow numpy jsonschema')

import numpy as np
import jsonschema
import rt_tool as rt
from PIL import Image, ImageCms

PP3 = '[Version]\nAppVersion=5.13\n[Exposure]\nAuto=false\nCompensation=0\n[Color Management]\nWorkingProfile=Rec2020\nOutputProfile=RTv4_sRGB\n'

class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'input with spaces;$.NEF'
        self.source.write_bytes(b'II\x2a\x00synthetic TIFF-header-only mock fixture, not a real decoded RAW')
        self.pp3 = self.root / 'approved.pp3'
        self.pp3.write_text(PP3)
        self.request = {'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
                        'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                        'output_dir': str(self.root / 'out'), 'format': 'jpeg', 'exposure_ev': .3}

    def error(self, code, fn, *args):
        with self.assertRaises(rt.ToolError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def fake_render(self, argv, **kwargs):
        self.assertEqual(argv[-2], '-c')
        self.assertTrue(Path(argv[-1]).is_file())
        self.assertNotIn('-Y', argv)
        self.assertIn('-q', argv)
        self.assertIn('-js3', argv)
        self.assertEqual(kwargs['timeout'], 300)
        out = Path(argv[argv.index('-o') + 1])
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB', (64, 32), (128, 64, 32)).save(out, icc_profile=icc)
        return 'mock renderer log'

    def render_dispatch(self, req):
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            return rt.dispatch(req)

    def render(self):
        return self.render_dispatch(self.request)

    def test_exiftool_install_guidance(self):
        with patch.object(rt.shutil, 'which', return_value=None), patch.object(rt.platform, 'system', return_value='Darwin'), patch.object(rt, 'engine', return_value={'version':'5.13'}):
            report = rt.dispatch({'action':'doctor'})
            self.assertIn('brew install exiftool', report['exiftool_install_hint'])
            with self.assertRaises(rt.ToolError) as caught:
                rt.dispatch({'action':'inspect','source':str(self.source)})
            self.assertEqual(caught.exception.code, 'DEPENDENCY_MISSING')
            self.assertIn('brew install exiftool', str(caught.exception))
        with patch.object(rt.platform, 'system', return_value='Linux'):
            self.assertNotIn('brew install', rt.exiftool_hint())

    def test_inspect_source_type_and_metadata(self):
        # 1. JPEG inspect
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        sample_jpg = self.root / 'inspect_sample.JPG'
        im = Image.new('RGB', (64, 48), (100, 150, 200))
        exif = im.getexif()
        exif[0x0112] = 3
        im.save(sample_jpg, 'JPEG', icc_profile=icc, exif=exif)

        with patch.object(rt.shutil, 'which', return_value='/fake/exiftool'), patch.object(rt, 'run', return_value='[{"SourceFile":"dummy"}]'):
            res_jpg = rt.dispatch({'action': 'inspect', 'source': str(sample_jpg)})
            self.assertEqual(res_jpg['source_type'], 'jpeg')
            self.assertTrue(res_jpg['has_icc'])
            self.assertEqual(res_jpg['orientation'], 3)
            self.assertEqual(res_jpg['sha256'], rt.digest(sample_jpg))

            # 2. RAW inspect
            res_raw = rt.dispatch({'action': 'inspect', 'source': str(self.source)})
            self.assertEqual(res_raw['source_type'], 'raw')
            self.assertNotIn('has_icc', res_raw)
            self.assertEqual(res_raw['sha256'], rt.digest(self.source))

    def test_tiff_qc_precision_boundary(self):
        path = self.root / '16bit.tif'
        Image.new('I;16', (16,16), 1024).save(path)
        result = rt.qc(path)
        self.assertEqual(result['file_bits_per_sample'], [16])
        self.assertFalse(result['precision_verified'])
        self.assertIn('8-bit', result['measurement'])
        self.assertIn('not RAW clipping', result['qc_scope'])

    def test_starter_profile(self):
        path = Path(__file__).parents[1] / 'assets/neutral-rec2020-rt513.pp3'
        meta, text = rt.profile(path)
        self.assertEqual(meta['working_profile'], 'Rec2020')
        self.assertEqual(meta['output_profile'], 'RTv4_sRGB')
        self.assertEqual(meta['app_version'], '5.13')
        self.assertTrue(meta['exposure_patchable'])

    def test_schema_matches_export(self):
        self.assertEqual(rt.schema(), json.loads((Path(__file__).parents[1] / 'references/tool-schema.json').read_text()))

    def test_reject_bad_requests(self):
        for value in [3, -3, float('nan'), float('inf'), True, '0.2']:
            self.error('INVALID_REQUEST', rt.validate, {**self.request, 'exposure_ev': value})
        self.error('INVALID_REQUEST', rt.validate, {**self.request, 'extra_args': '-Y'})
        self.error('INVALID_REQUEST', rt.validate, {'action': 'render'})
        self.error('INVALID_REQUEST', rt.validate, {'action': []})

    def test_patch_only_one_field(self):
        self.assertEqual(rt.patch_exposure(PP3, .3), PP3.replace('Compensation=0', 'Compensation=0.3'))
        self.error('PROFILE_INVALID', rt.patch_exposure, '[Exposure]\n', .3)
        self.error('PROFILE_INVALID', rt.patch_exposure, PP3.replace('Compensation=0', 'Compensation=0\nCompensation=1'), .3)

    def test_missing_colorspace(self):
        self.pp3.write_text(PP3.replace('WorkingProfile=Rec2020\n', ''))
        self.error('PROFILE_NOT_PINNED', rt.profile, str(self.pp3))

    def test_profile_hash_mismatch(self):
        self.request['profile_sha256'] = '0' * 64
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}):
            self.error('PROFILE_CHANGED', rt.dispatch, self.request)

    def test_engine_version_mismatch(self):
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.12'}):
            self.error('VERSION_MISMATCH', rt.dispatch, self.request)

    def test_render_manifest_no_overwrite(self):
        before = rt.digest(self.source)
        one, two = self.render(), self.render()
        self.assertNotEqual(one['output'], two['output'])
        self.assertEqual(before, rt.digest(self.source))
        manifest = json.loads(Path(one['manifest']).read_text())
        self.assertEqual(manifest['source_sha256'], before)
        self.assertEqual(manifest['output_sha256'], rt.digest(one['output']))
        self.assertEqual(manifest['visual_review'], 'pending')
        self.assertEqual(one['qc']['width'], 64)
        self.assertTrue(one['qc']['icc_sha256'])
        self.assertIn('Compensation=0.3', (Path(one['output']).parent / 'effective.pp3').read_text())

    def test_failed_render_cleanup(self):
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=rt.ToolError('PROCESS_FAILED', 'fake failure')):
            self.error('PROCESS_FAILED', rt.dispatch, self.request)
        self.assertEqual(list((self.root / 'out').iterdir()), [])
        self.assertTrue(self.source.is_file())

    def test_no_icc_rejected(self):
        def no_icc(argv, **kwargs):
            Image.new('RGB', (8, 8)).save(argv[argv.index('-o') + 1])
            return ''
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=no_icc):
            self.error('OUTPUT_INVALID', rt.dispatch, self.request)

    def test_subprocess_no_shell_and_timeout(self):
        with patch.object(rt.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')) as mock:
            self.assertEqual(rt.run(['/fake/rt', str(self.source)]), 'ok')
            self.assertIs(mock.call_args.kwargs['shell'], False)
            self.assertEqual(mock.call_args.args[0][1], str(self.source))
        with patch.object(rt.subprocess, 'run', side_effect=subprocess.TimeoutExpired('rt', 30)):
            self.error('TIMEOUT', rt.run, ['/fake/rt'])

    def test_help_nonzero_only_for_probe(self):
        with patch.object(rt.subprocess, 'run', return_value=subprocess.CompletedProcess([], 255, 'help', '')):
            self.assertEqual(rt.run(['/fake/rt', '-h'], help_probe=True), 'help')
            self.error('PROCESS_FAILED', rt.run, ['/fake/rt', '-h'])

    def test_invalid_json_cli(self):
        import sys
        proc = subprocess.run([sys.executable, str(Path(rt.__file__)), '--request', '-'],
                              input='{"action":"render","unknown":1}', capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)['error']['code'], 'INVALID_REQUEST')

    def test_jpeg_case_insensitive_and_manifest(self):
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        for ext in ['.jpg', '.JPG', '.jpeg', '.JPEG']:
            jpg_file = self.root / f'sample{ext}'
            im = Image.new('RGB', (64, 48), (100, 150, 200))
            exif = im.getexif()
            exif[0x0112] = 6
            im.save(jpg_file, 'JPEG', icc_profile=icc, exif=exif)

            req = {**self.request, 'source': str(jpg_file)}
            before_hash = rt.digest(jpg_file)

            with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
                res = rt.dispatch(req)
                self.assertEqual(res['source_type'], 'jpeg')
                self.assertEqual(before_hash, rt.digest(jpg_file))

                manifest = json.loads(Path(res['manifest']).read_text())
                self.assertEqual(manifest['source_type'], 'jpeg')
                self.assertTrue(manifest['source_has_icc'])
                self.assertEqual(manifest['source_orientation'], 6)
                self.assertEqual(manifest['source_sha256'], before_hash)

    def test_reject_masqueraded_jpeg(self):
        fake1 = self.root / 'fake1.jpg'
        fake1.write_bytes(b'plain text not an image')
        self.error('CORRUPT_INPUT', rt.dispatch, {**self.request, 'source': str(fake1)})

        fake2 = self.root / 'fake2.jpg'
        Image.new('RGB', (16, 16)).save(fake2, 'PNG')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {**self.request, 'source': str(fake2)})

        fake3 = self.root / 'fake3.png'
        Image.new('RGB', (16, 16)).save(fake3, 'PNG')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {**self.request, 'source': str(fake3)})

    def test_reject_corrupt_jpeg(self):
        corrupt = self.root / 'corrupt.jpg'
        temp_jpg = self.root / 'temp.jpg'
        Image.new('RGB', (100, 100), (255, 0, 0)).save(temp_jpg, 'JPEG')
        corrupt.write_bytes(temp_jpg.read_bytes()[:150])
        self.error('CORRUPT_INPUT', rt.dispatch, {**self.request, 'source': str(corrupt)})

    def test_raw_regression(self):
        before = rt.digest(self.source)
        res = self.render()
        self.assertEqual(res['source_type'], 'raw')
        manifest = json.loads(Path(res['manifest']).read_text())
        self.assertEqual(manifest['source_type'], 'raw')
        self.assertEqual(manifest['source_sha256'], before)

    def test_actual_rawtherapee_jpeg_render(self):
        try:
            eng = rt.engine()
        except rt.ToolError:
            self.skipTest('RawTherapee CLI engine not available on this machine')

        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        synth_jpg = self.root / 'synth_oriented.jpg'
        im = Image.new('RGB', (120, 80), (200, 120, 60))
        exif = im.getexif()
        exif[0x0112] = 6
        im.save(synth_jpg, 'JPEG', icc_profile=icc, exif=exif)
        synth_hash_before = rt.digest(synth_jpg)

        pp3_path = Path(__file__).parents[1] / 'assets/neutral-rec2020-rt513.pp3'
        pp3_meta, _ = rt.profile(pp3_path)

        # 1. Real render to JPEG
        req_jpg = {
            'action': 'render',
            'source': str(synth_jpg),
            'profile': str(pp3_path),
            'profile_sha256': pp3_meta['sha256'],
            'engine_version': eng['version'],
            'output_dir': str(self.root / 'out_real_jpg'),
            'format': 'jpeg',
            'exposure_ev': 0.3
        }
        res_jpg = rt.dispatch(req_jpg)
        self.assertEqual(res_jpg['source_type'], 'jpeg')
        self.assertTrue(Path(res_jpg['output']).is_file())
        self.assertEqual(rt.digest(synth_jpg), synth_hash_before)
        with Image.open(res_jpg['output']) as out_im:
            self.assertEqual(out_im.size, (80, 120))
            self.assertTrue(out_im.info.get('icc_profile'))
        manifest_jpg = json.loads(Path(res_jpg['manifest']).read_text())
        self.assertEqual(manifest_jpg['source_orientation'], 6)
        self.assertTrue(manifest_jpg['source_has_icc'])
        self.assertEqual(manifest_jpg['source_sha256'], synth_hash_before)

        # 2. Real render to TIFF16
        req_tif = {
            'action': 'render',
            'source': str(synth_jpg),
            'profile': str(pp3_path),
            'profile_sha256': pp3_meta['sha256'],
            'engine_version': eng['version'],
            'output_dir': str(self.root / 'out_real_tif'),
            'format': 'tiff16',
            'exposure_ev': -0.2
        }
        res_tif = rt.dispatch(req_tif)
        self.assertEqual(res_tif['source_type'], 'jpeg')
        self.assertTrue(Path(res_tif['output']).is_file())
        self.assertEqual(rt.digest(synth_jpg), synth_hash_before)
        with Image.open(res_tif['output']) as out_im:
            self.assertEqual(out_im.size, (80, 120))
            self.assertEqual(out_im.tag_v2.get(258), (16, 16, 16))
            self.assertTrue(out_im.info.get('icc_profile'))
        self.assertEqual(res_tif['qc']['file_bits_per_sample'], [16, 16, 16])
        self.assertFalse(res_tif['qc']['precision_verified'])
        for rendered in (res_jpg, res_tif):
            with Image.open(rendered['preview']['path']) as preview:
                self.assertEqual(preview.size, (80, 120))
                self.assertEqual(preview.format, 'JPEG')
                self.assertTrue(preview.info.get('icc_profile'))
            self.assertEqual(rt.digest(rendered['preview']['path']), rendered['preview']['sha256'])

    def test_actual_rawtherapee_two_round_incremental_wb(self):
        try:
            eng = rt.engine()
        except rt.ToolError:
            self.skipTest('RawTherapee CLI engine not available on this machine')

        # Create synthetic test JPEG in temp dir (no user photos)
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        synth_jpg = self.root / 'synth_wb_test.jpg'
        im = Image.new('RGB', (100, 100), (180, 140, 100))
        im.save(synth_jpg, 'JPEG', icc_profile=icc)

        pp3_path = Path(__file__).parents[1] / 'assets/neutral-rec2020-rt513.pp3'
        pp3_meta, _ = rt.profile(pp3_path)

        sess_file = self.root / 'synth_sess.json'
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file),
            'source': str(synth_jpg),
            'base_profile': str(pp3_path),
            'confirmed_brief': '合成图两轮白平衡验证'
        })
        self.assertEqual(init_res['revision'], 0)

        # Round 1: Full WB (temperature: 5800, green: 1.05) rendered via real RawTherapee
        out_r1 = self.root / 'out_round1'
        req_r1 = {
            'action': 'render',
            'source': str(synth_jpg),
            'profile': str(pp3_path),
            'profile_sha256': pp3_meta['sha256'],
            'engine_version': eng['version'],
            'output_dir': str(out_r1),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'adjustments': {
                'white_balance': {
                    'temperature': 5800,
                    'green': 1.05
                }
            }
        }
        res_r1 = rt.dispatch(req_r1)
        self.assertTrue(Path(res_r1['output']).is_file())
        self.assertEqual(res_r1['session_revision'], 1)

        # Verify Round 1 Manifest on disk and Session History hash match
        man_r1_path = Path(res_r1['manifest'])
        self.assertTrue(man_r1_path.is_file())
        man_r1_data = json.loads(man_r1_path.read_text())
        self.assertEqual(man_r1_data['effective_adjustments']['white_balance'], {'temperature': 5800, 'green': 1.05})
        man_r1_disk_hash = rt.digest(man_r1_path)
        sess_data_r1 = json.loads(sess_file.read_text())
        self.assertEqual(sess_data_r1['effective_adjustments']['white_balance'], {'temperature': 5800, 'green': 1.05})
        self.assertEqual(sess_data_r1['history'][-1]['manifest_sha256'], man_r1_disk_hash)
        self.assertEqual(sess_data_r1['history'][-1]['manifest_path'], str(man_r1_path))

        # Round 2: Incremental WB (only temperature: 5200) rendered via real RawTherapee
        out_r2 = self.root / 'out_round2'
        req_r2 = {
            'action': 'render',
            'source': str(synth_jpg),
            'profile': str(pp3_path),
            'profile_sha256': pp3_meta['sha256'],
            'engine_version': eng['version'],
            'output_dir': str(out_r2),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 1,
            'adjustments': {
                'white_balance': {
                    'temperature': 5200
                }
            }
        }
        res_r2 = rt.dispatch(req_r2)
        self.assertTrue(Path(res_r2['output']).is_file())
        self.assertEqual(res_r2['session_revision'], 2)

        # CRITICAL VERIFICATION: green: 1.05 must be inherited!
        man_r2_path = Path(res_r2['manifest'])
        self.assertTrue(man_r2_path.is_file())
        man_r2_data = json.loads(man_r2_path.read_text())
        self.assertEqual(man_r2_data['effective_adjustments']['white_balance'], {'temperature': 5200, 'green': 1.05})

        # Verify Round 2 Manifest on disk and Session History hash match
        man_r2_disk_hash = rt.digest(man_r2_path)
        sess_data_r2 = json.loads(sess_file.read_text())
        self.assertEqual(sess_data_r2['effective_adjustments']['white_balance'], {'temperature': 5200, 'green': 1.05})
        self.assertEqual(sess_data_r2['history'][-1]['manifest_sha256'], man_r2_disk_hash)
        self.assertEqual(sess_data_r2['history'][-1]['manifest_path'], str(man_r2_path))

        # Verify PP3 written to folder for round 2 contains inherited green and new temperature
        r2_folder = Path(res_r2['output']).parent
        pp3_files = list(r2_folder.glob('*.pp3'))
        self.assertTrue(len(pp3_files) > 0)
        r2_pp3_content = pp3_files[0].read_text()
        self.assertIn('Temperature=5200', r2_pp3_content)
        self.assertIn('Green=1.05', r2_pp3_content)

    def test_capabilities_action(self):
        res = rt.dispatch({'action': 'capabilities'})
        self.assertEqual(res['tool_version'], rt.VERSION)
        self.assertIn('exposure_ev', res['controllable_parameters'])
        self.assertIn('white_balance', res['controllable_parameters'])
        self.assertIn('tone_curve', res['controllable_parameters'])
        self.assertIn('denoise', res['controllable_parameters'])
        self.assertIn('sharpen', res['controllable_parameters'])
        self.assertIn('local_mask_brush', res['unsupported_capabilities'])

        res_jpeg = rt.dispatch({'action': 'capabilities', 'source_type': 'jpeg'})
        self.assertEqual(res_jpeg['filtered_for_source_type'], 'jpeg')
        self.assertIn('Baked 8-bit', res_jpeg['effective_restriction'])

        self.error('INVALID_REQUEST', rt.dispatch, {'action': 'capabilities', 'source_type': 'png'})

    def test_adjustments_validation_bounds_and_types(self):
        base_req = dict(self.request)

        # Contrast bounds and types
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'contrast': 101}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'contrast': -101}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'contrast': True}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'contrast': 10.5}})

        # Saturation bounds and types
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'saturation': 101}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'saturation': False}})

        # White balance bounds, pairs, and types
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': 5500}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'green': 1.0}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': 1499, 'green': 1.0}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': 60001, 'green': 1.0}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': 5500, 'green': 0.49}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': 5500, 'green': 2.01}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': True, 'green': 1.0}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'white_balance': {'temperature': 5500, 'green': float('nan')}}})

        # Denoise bounds and types
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'denoise': {'luma': -1.0}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'denoise': {'luma': 101.0}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'denoise': {'chroma': -0.1}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'denoise': {'luma': True}}})

        # Sharpen bounds and types
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'sharpen': {'amount': 9}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'sharpen': {'amount': 501}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'sharpen': {'radius': 0.29}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'sharpen': {'radius': 3.01}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'sharpen': {'amount': False}}})

        # Unknown field in adjustments
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'unsupported_tool': 123}})

    def test_tone_curve_validation(self):
        base_req = dict(self.request)

        # Case-insensitive mode normalization, while unknown modes remain invalid.
        normalized = self.render_dispatch({**base_req, 'adjustments': {'tone_curve': {'mode': 'standard', 'points': [[0.0, 0.0], [1.0, 1.0]]}}})
        normalized_pp3 = (Path(normalized['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('CurveMode=Standard', normalized_pp3)
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Custom', 'points': [[0.0, 0.0], [1.0, 1.0]]}}})

        # Invalid points length (<2 or >16)
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0]]}}})
        too_many = [[i / 17, i / 17] for i in range(18)]
        too_many[0] = [0.0, 0.0]
        too_many[-1] = [1.0, 1.0]
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': too_many}}})

        # Endpoints not 0.0 or 1.0
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.1, 0.0], [1.0, 1.0]]}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.9, 1.0]]}}})

        # x not strictly increasing
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.5, 0.5], [0.5, 0.6], [1.0, 1.0]]}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.6, 0.5], [0.4, 0.6], [1.0, 1.0]]}}})

        # y descending
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.5], [0.5, 0.2], [1.0, 1.0]]}}})

        # Boolean masquerading in points
        self.error('INVALID_REQUEST', rt.dispatch, {**base_req, 'adjustments': {'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [True, 0.5], [1.0, 1.0]]}}})

    def test_duplicate_exposure_rejection(self):
        req = dict(self.request)
        req['exposure_ev'] = 0.3
        req['adjustments'] = {'exposure_ev': -0.2}
        self.error('INVALID_REQUEST', rt.dispatch, req)

    def test_engine_version_rejection_for_new_adjustments(self):
        pp3_512 = self.root / 'profile_512.pp3'
        pp3_512.write_text('[Version]\nAppVersion=5.12\n[Exposure]\nAuto=false\nCompensation=0\n[Color Management]\nWorkingProfile=Rec2020\nOutputProfile=RTv4_sRGB\n')
        req = dict(self.request)
        del req['exposure_ev']
        req['profile'] = str(pp3_512)
        req['profile_sha256'] = rt.digest(pp3_512)
        req['engine_version'] = '5.12'
        req['adjustments'] = {'contrast': 10}
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.12'}):
            self.error('UNSUPPORTED_ADJUSTMENTS', rt.dispatch, req)

    def test_mock_render_with_all_adjustments(self):
        req = dict(self.request)
        del req['exposure_ev']
        req['adjustments'] = {
            'exposure_ev': 0.2,
            'contrast': 15,
            'saturation': -5,
            'white_balance': {'temperature': 5800, 'green': 1.05},
            'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]]},
            'denoise': {'enabled': True, 'luma': 20.0, 'chroma': 35.0},
            'sharpen': {'enabled': True, 'amount': 250, 'radius': 0.8}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res = rt.dispatch(req)
            self.assertTrue(Path(res['output']).is_file())
            manifest = json.loads(Path(res['manifest']).read_text())
            self.assertEqual(manifest['request']['adjustments']['contrast'], 15)

    def test_real_rawtherapee_pixel_effects_across_all_domains(self):
        try:
            eng = rt.engine()
        except rt.ToolError:
            self.skipTest('RawTherapee CLI engine not available on this machine')

        from PIL import ImageChops, ImageStat

        # Create synthetic JPEG test pattern with gradients and noise
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        synth_jpg = self.root / 'synth_pattern.jpg'
        im = Image.new('RGB', (64, 64))
        for x in range(64):
            for y in range(64):
                noise = (x * 7 + y * 13) % 25 - 12
                r = min(255, max(0, x * 4 + noise))
                g = min(255, max(0, y * 4 + noise))
                b = min(255, max(0, 128 + noise))
                im.putpixel((x, y), (r, g, b))
        im.save(synth_jpg, 'JPEG', icc_profile=icc)

        pp3_path = Path(__file__).parents[1] / 'assets/neutral-rec2020-rt513.pp3'
        pp3_meta, _ = rt.profile(pp3_path)

        def do_render(adj, folder_name, ev=None):
            req = {
                'action': 'render',
                'source': str(synth_jpg),
                'profile': str(pp3_path),
                'profile_sha256': pp3_meta['sha256'],
                'engine_version': eng['version'],
                'output_dir': str(self.root / folder_name),
                'format': 'jpeg'
            }
            if ev is not None:
                req['exposure_ev'] = ev
            if adj is not None:
                req['adjustments'] = adj
            res = rt.dispatch(req)
            self.assertTrue(Path(res['output']).is_file())
            return Image.open(res['output']).copy()

        # 0. Base render
        base_im = do_render(None, 'out_base')

        # 1. Legacy exposure_ev
        im_ev = do_render(None, 'out_ev', ev=0.8)
        diff_ev = ImageChops.difference(base_im, im_ev)
        self.assertIsNotNone(diff_ev.getbbox(), 'Legacy exposure_ev must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_ev).mean), 10)

        # 2. Contrast
        im_contrast = do_render({'contrast': 50}, 'out_contrast')
        diff_c = ImageChops.difference(base_im, im_contrast)
        self.assertIsNotNone(diff_c.getbbox(), 'Contrast adjustment must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_c).mean), 10)

        # 3. Saturation
        im_sat = do_render({'saturation': 50}, 'out_sat')
        diff_s = ImageChops.difference(base_im, im_sat)
        self.assertIsNotNone(diff_s.getbbox(), 'Saturation adjustment must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_s).mean), 10)

        # 4. White Balance (Temperature and Green)
        im_wb = do_render({'white_balance': {'temperature': 3500, 'green': 1.2}}, 'out_wb')
        diff_wb = ImageChops.difference(base_im, im_wb)
        self.assertIsNotNone(diff_wb.getbbox(), 'White balance adjustment must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_wb).mean), 20)

        # 5. Tone Curve
        im_curve = do_render({'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.5, 0.8], [1.0, 1.0]]}}, 'out_curve')
        diff_curve = ImageChops.difference(base_im, im_curve)
        self.assertIsNotNone(diff_curve.getbbox(), 'Tone curve adjustment must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_curve).mean), 20)

        # 6. Sharpening
        im_sharp = do_render({'sharpen': {'enabled': True, 'amount': 500, 'radius': 2.0}}, 'out_sharp')
        diff_sharp = ImageChops.difference(base_im, im_sharp)
        self.assertIsNotNone(diff_sharp.getbbox(), 'Sharpening adjustment must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_sharp).mean), 10)

        # 7. Denoising
        im_denoise = do_render({'denoise': {'enabled': True, 'luma': 80.0, 'chroma': 80.0}}, 'out_denoise')
        diff_denoise = ImageChops.difference(base_im, im_denoise)
        self.assertIsNotNone(diff_denoise.getbbox(), 'Denoising adjustment must alter pixels')
        self.assertGreater(sum(ImageStat.Stat(diff_denoise).mean), 10)

    def test_session_lifecycle_independent_process(self):
        sess_file = self.root / 'user_project' / 'photo_session.json'
        cli_py = Path(__file__).parent / 'rt_tool.py'

        # 1. Independent CLI process: init
        req_init = {
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_file),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': '社交分享；逆光人像面部偏暗；自然真实通透；保留现场暖黄氛围与发丝质感',
            'keep': ['保留现场暖黄氛围'],
            'change': ['面部稍作提亮']
        }
        p_init = subprocess.run(['python3', str(cli_py), '--request', '-'],
                                input=json.dumps(req_init), text=True, capture_output=True)
        self.assertEqual(p_init.returncode, 0, p_init.stderr)
        res_init = json.loads(p_init.stdout)
        self.assertTrue(res_init['ok'])
        self.assertEqual(res_init['result']['revision'], 0)
        self.assertTrue(sess_file.is_file())

        # 2. Independent CLI process: record_feedback with full keep/change replacement & user_feedback
        req_fb = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'keep': ['保留现场暖黄氛围', '夕阳高光未溢出满意'],
            'change': ['面部阴影再提亮半档', '下巴暗部彩噪消除'],
            'user_feedback': '很满意夕阳暖色，请务必保留暖色，但把面部阴影再提亮半档并消下巴彩噪',
            'adjustments': {'exposure_ev': 0.4, 'contrast': 5}
        }
        p_fb = subprocess.run(['python3', str(cli_py), '--request', '-'],
                              input=json.dumps(req_fb), text=True, capture_output=True)
        self.assertEqual(p_fb.returncode, 0, p_fb.stderr)
        res_fb = json.loads(p_fb.stdout)
        self.assertTrue(res_fb['ok'])
        self.assertEqual(res_fb['result']['revision'], 1)
        self.assertEqual(len(res_fb['result']['keep']), 2)

        # 3. Independent CLI process: load restores warm color keep and change
        req_load = {
            'action': 'session',
            'operation': 'load',
            'session_path': str(sess_file)
        }
        p_load = subprocess.run(['python3', str(cli_py), '--request', '-'],
                                input=json.dumps(req_load), text=True, capture_output=True)
        self.assertEqual(p_load.returncode, 0, p_load.stderr)
        res_load = json.loads(p_load.stdout)
        self.assertTrue(res_load['ok'])
        sess = res_load['result']['session']
        self.assertEqual(sess['revision'], 1)
        self.assertIn('夕阳高光未溢出满意', sess['keep'])
        self.assertIn('面部阴影再提亮半档', sess['change'])
        self.assertEqual(sess['effective_adjustments']['exposure_ev'], 0.4)
        self.assertEqual(len(sess['history']), 1)
        self.assertEqual(sess['history'][0]['revision'], 0)
        self.assertIn('很满意夕阳暖色', sess['history'][0]['user_feedback'])

        # 4. Partial feedback update: omitting keep must NOT wipe keep
        req_fb2 = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_file),
            'expected_revision': 1,
            'change': ['仅微调锐化']
        }
        res_fb2 = rt.dispatch(req_fb2)
        self.assertEqual(res_fb2['revision'], 2)
        # Verify keep is still preserved, not cleared to empty!
        self.assertEqual(len(res_fb2['keep']), 2)
        self.assertEqual(res_fb2['change'], ['仅微调锐化'])

    def test_session_init_reentrancy_refuses_overwrite(self):
        sess_file = self.root / 'existing_session.json'
        req_init = {
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_file),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': '首次确认需求'
        }
        res1 = rt.dispatch(req_init)
        self.assertEqual(res1['revision'], 0)
        before_text = sess_file.read_text(encoding='utf-8')

        # Re-entrant init on same path must be strictly rejected
        self.error('SESSION_EXISTS', rt.dispatch, req_init)
        # Content remains completely unmodified
        self.assertEqual(sess_file.read_text(encoding='utf-8'), before_text)

    def test_session_concurrent_writes_revision_conflict(self):
        import concurrent.futures

        sess_file = self.root / 'concurrent_session.json'
        rt.dispatch({
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_file),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': '并发测试初始状态'
        })

        req1 = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'change': ['并发变更 A']
        }
        req2 = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'change': ['并发变更 B']
        }

        results = []
        def try_write(r):
            try:
                return ('ok', rt.dispatch(r))
            except rt.ToolError as e:
                return ('error', e.code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(try_write, req1), executor.submit(try_write, req2)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        statuses = [r[0] for r in results]
        self.assertIn('ok', statuses)
        self.assertIn('error', statuses)
        error_res = next(r for r in results if r[0] == 'error')
        self.assertEqual(error_res[1], 'REVISION_CONFLICT')

        # Check final session state: revision is exactly 1, history length is exactly 1
        data = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(data['revision'], 1)
        self.assertEqual(len(data['history']), 1)

    def test_session_corrupt_and_unsupported_schema(self):
        sess_file = self.root / 'corrupted_session.json'

        # 1. Corrupt JSON
        sess_file.write_text('{invalid json syntax: ...', encoding='utf-8')
        self.error('CORRUPT_SESSION', rt.dispatch, {'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})
        self.error('CORRUPT_SESSION', rt.dispatch, {'action': 'session', 'operation': 'record_feedback', 'session_path': str(sess_file), 'expected_revision': 0})
        # File is preserved as-is, never deleted or reset
        self.assertEqual(sess_file.read_text(encoding='utf-8'), '{invalid json syntax: ...')

        # 2. Unsupported schema version
        sess_file.write_text(json.dumps({'schema_version': 999, 'revision': 0}), encoding='utf-8')
        self.error('UNSUPPORTED_SESSION_SCHEMA', rt.dispatch, {'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})
        self.error('UNSUPPORTED_SESSION_SCHEMA', rt.dispatch, {'action': 'session', 'operation': 'record_feedback', 'session_path': str(sess_file), 'expected_revision': 0})
        # File is preserved as-is
        self.assertEqual(json.loads(sess_file.read_text(encoding='utf-8'))['schema_version'], 999)

    def test_session_atomic_write_mid_failure_preserves_old_file(self):
        sess_file = self.root / 'atomic_session.json'
        rt.dispatch({
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_file),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': '原子写入测试基准'
        })
        original_bytes = sess_file.read_bytes()

        req_fb = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'change': ['测试写入中途崩溃']
        }

        # Simulate catastrophic disk/filesystem failure during os.replace
        with patch('os.replace', side_effect=IOError('Disk full or sudden failure')):
            with self.assertRaises(IOError):
                rt.dispatch(req_fb)

        # Original session file must be 100% intact with original bytes
        self.assertEqual(sess_file.read_bytes(), original_bytes)
        loaded = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(loaded['revision'], 0)
        self.assertEqual(len(loaded['history']), 0)

    def test_bound_render_lifecycle_and_intent_continuity(self):
        sess_file = self.root / 'bound_project' / 'photo_session.json'
        cli_py = Path(__file__).parent / 'rt_tool.py'

        # 0. Init session
        req_init = {
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_file),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': '日常社交分享；主体逆光人像；自然真实；保留现场暖黄氛围与发丝',
            'keep': ['保留现场暖黄氛围'],
            'change': ['面部稍作提亮']
        }
        res_init = rt.dispatch(req_init)
        self.assertEqual(res_init['revision'], 0)

        # 1. User feedback record
        req_fb = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'keep': ['现场背景暖黄氛围满意', '夕阳高光未溢出满意'],
            'change': ['面部阴影提亮半档'],
            'user_feedback': '很满意暖色，请保持暖黄氛围，但把人脸提亮半档'
        }
        res_fb = rt.dispatch(req_fb)
        self.assertEqual(res_fb['revision'], 1)

        # 2. Round 1 Bound Render (WB + tone curve)
        req_render1 = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_round1'),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 1,
            'adjustments': {
                'white_balance': {'temperature': 5800, 'green': 1.05},
                'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.5, 0.7], [1.0, 1.0]]}
            }
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res_r1 = rt.dispatch(req_render1)
            self.assertEqual(res_r1['session_revision'], 2)
            manifest1 = json.loads(Path(res_r1['manifest']).read_text(encoding='utf-8'))
            self.assertEqual(manifest1['commit_status'], 'reference')
            self.assertEqual(manifest1['commit_authority'], 'session_history')
            self.assertEqual(manifest1['input_revision'], 1)
            self.assertIn('很满意暖色', manifest1['intent']['user_feedback'])
            self.assertEqual(manifest1['effective_adjustments']['white_balance']['temperature'], 5800)

        # 3. Independent CLI process load verification
        req_load = {'action': 'session', 'operation': 'load', 'session_path': str(sess_file)}
        p_load = subprocess.run(['python3', str(cli_py), '--request', '-'],
                                input=json.dumps(req_load), text=True, capture_output=True)
        self.assertEqual(p_load.returncode, 0, p_load.stderr)
        sess_loaded = json.loads(p_load.stdout)['result']['session']
        self.assertEqual(sess_loaded['revision'], 2)
        self.assertEqual(sess_loaded['effective_adjustments']['white_balance']['temperature'], 5800)
        self.assertEqual(len(sess_loaded['history']), 2)

        # History SHA-256 matches immutable manifest on disk exactly
        h1 = sess_loaded['history'][-1]
        self.assertEqual(h1['manifest_sha256'], rt.digest(Path(res_r1['manifest'])))
        self.assertEqual(h1['output_sha256'], rt.digest(Path(res_r1['output'])))

        # 4. Round 2 Bound Render: only specify contrast: 20 -> WB and tone_curve must be preserved!
        req_render2 = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_round2'),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 2,
            'adjustments': {'contrast': 20}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res_r2 = rt.dispatch(req_render2)
            self.assertEqual(res_r2['session_revision'], 3)
            manifest2 = json.loads(Path(res_r2['manifest']).read_text(encoding='utf-8'))
            self.assertEqual(manifest2['commit_status'], 'reference')
            self.assertEqual(manifest2['commit_authority'], 'session_history')
            self.assertEqual(manifest2['input_revision'], 2)
            # Both round 1 adjustments AND round 2 contrast are present!
            self.assertEqual(manifest2['effective_adjustments']['contrast'], 20)
            self.assertEqual(manifest2['effective_adjustments']['white_balance']['temperature'], 5800)
            self.assertEqual(manifest2['effective_adjustments']['tone_curve']['mode'], 'Standard')

    def test_bound_render_reset_adjustments(self):
        sess_file = self.root / 'reset_project' / 'photo_session.json'

        # Init & set initial adjustments
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '重置测试'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'contrast': 30, 'white_balance': {'temperature': 6200, 'green': 1.1}}
        })

        # Render with reset_adjustments: True and only contrast: 10
        req = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_reset'),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 1,
            'reset_adjustments': True,
            'adjustments': {'contrast': 10}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res = rt.dispatch(req)
            self.assertEqual(res['session_revision'], 2)
            manifest = json.loads(Path(res['manifest']).read_text(encoding='utf-8'))
            # Previous white_balance must be gone, only contrast: 10 remains!
            self.assertEqual(manifest['effective_adjustments'], {'contrast': 10})

    def test_bound_render_mismatches_and_stale_revision_rejected(self):
        sess_file = self.root / 'mismatch_project' / 'photo_session.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '不一致拦截测试'
        })

        base_req = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_mismatch'),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 0
        }

        # 1. Stale revision
        self.error('REVISION_CONFLICT', rt.dispatch, {**base_req, 'expected_revision': 99})

        # 2. Source path mismatch
        other_src = self.root / 'other.NEF'
        other_src.write_bytes(b'II\x2a\x00other mock content')
        self.error('SESSION_SOURCE_MISMATCH', rt.dispatch, {**base_req, 'source': str(other_src)})

        # 3. Source hash changed since init
        self.source.write_bytes(b'II\x2a\x00modified source content after init')
        self.error('SESSION_SOURCE_CHANGED', rt.dispatch, base_req)
        # Restore source
        self.source.write_bytes(b'II\x2a\x00synthetic TIFF-header-only mock fixture, not a real decoded RAW')

        # 4. Profile path mismatch
        other_pp3 = self.root / 'other.pp3'
        other_pp3.write_text(self.pp3.read_text())
        self.error('SESSION_PROFILE_MISMATCH', rt.dispatch, {**base_req, 'profile': str(other_pp3), 'profile_sha256': rt.digest(other_pp3)})

        # 5. Profile hash changed since init
        tampered_pp3 = self.root / 'tampered.pp3'
        tampered_pp3.write_text(self.pp3.read_text())
        sess_tampered = self.root / 'tampered_session.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_tampered), 'source': str(self.source), 'base_profile': str(tampered_pp3),
            'confirmed_brief': '篡改测试'
        })
        tampered_pp3.write_text(self.pp3.read_text() + '# comment')
        self.error('SESSION_PROFILE_CHANGED', rt.dispatch, {
            **base_req,
            'session_path': str(sess_tampered),
            'profile': str(tampered_pp3),
            'profile_sha256': rt.digest(tampered_pp3)
        })

    def test_bound_render_engine_failure_and_commit_failure_isolation(self):
        sess_file = self.root / 'fail_project' / 'photo_session.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '故障隔离测试'
        })

        req = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_fail'),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 0
        }

        # 1. Engine failure during run
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=rt.ToolError('PROCESS_FAILED', 'RT crashed')):
            self.error('PROCESS_FAILED', rt.dispatch, req)
        # Session state must NOT advance
        loaded = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(loaded['revision'], 0)
        self.assertEqual(len(loaded['history']), 0)

        # 2. Session commit failure (atomic_write_json fails)
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            with patch('rt_tool.atomic_write_json', side_effect=IOError('Disk full on session write')):
                self.error('SESSION_COMMIT_FAILED', rt.dispatch, req)

        # Session state was not advanced! Single commit authority remains intact
        loaded2 = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(loaded2['revision'], 0)
        self.assertEqual(len(loaded2['history']), 0)

        # Orphan manifest exists on disk for post-mortem diagnostics, marked with session_history authority
        orphan_manifests = list((self.root / 'out_fail').rglob('manifest.json'))
        self.assertEqual(len(orphan_manifests), 1)
        orphan_m = json.loads(orphan_manifests[0].read_text(encoding='utf-8'))
        self.assertEqual(orphan_m['commit_status'], 'reference')
        self.assertEqual(orphan_m['commit_authority'], 'session_history')
        # Crucially, session history does NOT have this manifest, proving it is uncommitted
        self.assertTrue(all(h.get('manifest_path') != str(orphan_manifests[0]) for h in loaded2['history']))

    def test_manifest_hash_consistency_and_single_commit_authority(self):
        sess_file = self.root / 'hash_consist' / 'photo_session.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'Hash一致性与单一提交权威测试'
        })

        req = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_hash_consist'),
            'format': 'jpeg',
            'session_path': str(sess_file),
            'expected_revision': 0,
            'adjustments': {'contrast': 12, 'saturation': 5}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res = rt.dispatch(req)

        # 1. Manifest on disk is immutable once written
        manifest_path = Path(res['manifest'])
        disk_hash = rt.digest(manifest_path)
        output_path = Path(res['output'])
        output_disk_hash = rt.digest(output_path)

        # 2. Session history is the authoritative commit record
        sess = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(sess['revision'], 1)
        self.assertEqual(len(sess['history']), 1)
        commit_entry = sess['history'][0]

        # 3. Exactly matching hash without any drift or mismatch
        self.assertEqual(commit_entry['manifest_sha256'], disk_hash)
        self.assertEqual(commit_entry['output_sha256'], output_disk_hash)
        self.assertEqual(commit_entry['status'], 'SUCCESS')

        # 4. Manifest properties reflect reference and session_history authority
        m_content = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(m_content['commit_status'], 'reference')
        self.assertEqual(m_content['commit_authority'], 'session_history')
        self.assertEqual(m_content['session_id'], sess['session_id'])

    def test_cross_process_concurrent_cli_writes(self):
        sess_file = self.root / 'cross_proc_session.json'
        cli_py = Path(__file__).parent / 'rt_tool.py'

        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '跨进程并发测试'
        })

        req1 = json.dumps({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0, 'change': ['进程 1 修改']
        })
        req2 = json.dumps({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0, 'change': ['进程 2 修改']
        })

        # Launch two OS-level CLI processes simultaneously
        p1 = subprocess.Popen(['python3', str(cli_py), '--request', '-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen(['python3', str(cli_py), '--request', '-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        out1, _ = p1.communicate(input=req1)
        out2, _ = p2.communicate(input=req2)

        ret1 = json.loads(out1)
        ret2 = json.loads(out2)

        # Exactly one must succeed, and the other must fail with REVISION_CONFLICT
        successes = [r for r in [ret1, ret2] if r.get('ok') is True]
        conflicts = [r for r in [ret1, ret2] if r.get('ok') is False and r.get('error', {}).get('code') == 'REVISION_CONFLICT']

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

        # Verify final session revision is exactly 1 and exactly 1 history item
        sess = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(sess['revision'], 1)
        self.assertEqual(len(sess['history']), 1)

    def test_incremental_wb_and_record_feedback_preserves_green(self):
        sess_file = self.root / 'incremental_wb_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '增量白平衡测试'
        })
        # Round 1: render with initial WB (5800, 1.05)
        req_r1 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_inc1'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'white_balance': {'temperature': 5800, 'green': 1.05}}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res1 = rt.dispatch(req_r1)
            self.assertEqual(res1['session_revision'], 1)

        # Round 2: render with ONLY temperature: 5200 -> green 1.05 must be preserved!
        req_r2 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_inc2'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {'white_balance': {'temperature': 5200}}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            res2 = rt.dispatch(req_r2)
            self.assertEqual(res2['session_revision'], 2)

        sess_loaded = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(sess_loaded['revision'], 2)
        wb2 = sess_loaded['effective_adjustments']['white_balance']
        self.assertEqual(wb2['temperature'], 5200)
        self.assertEqual(wb2['green'], 1.05)

        # Round 3: record_feedback with ONLY temperature: 4800 -> green 1.05 preserved
        res_fb = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 2,
            'adjustments': {'white_balance': {'temperature': 4800}},
            'user_feedback': '再冷一点'
        })
        self.assertEqual(res_fb['revision'], 3)
        self.assertEqual(res_fb['effective_adjustments']['white_balance']['temperature'], 4800)
        self.assertEqual(res_fb['effective_adjustments']['white_balance']['green'], 1.05)

        # Round 4: record_feedback with ONLY green: 1.15 -> temperature 4800 preserved
        res_fb2 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 3,
            'adjustments': {'white_balance': {'green': 1.15}},
            'user_feedback': '偏绿一点'
        })
        self.assertEqual(res_fb2['revision'], 4)
        self.assertEqual(res_fb2['effective_adjustments']['white_balance']['temperature'], 4800)
        self.assertEqual(res_fb2['effective_adjustments']['white_balance']['green'], 1.15)

    def test_incremental_wb_missing_fields_rejected_when_no_history_or_reset(self):
        sess_file = self.root / 'fresh_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '无历史测试'
        })

        # 1. Fresh session with no history: render passing only temperature -> rejected
        req_fresh_render = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_fresh'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'white_balance': {'temperature': 5200}}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}):
            self.error('INVALID_REQUEST', rt.dispatch, req_fresh_render)

        # 2. Fresh session with no history: record_feedback passing only temperature -> rejected
        req_fresh_fb = {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'white_balance': {'temperature': 5200}}
        }
        self.error('INVALID_REQUEST', rt.dispatch, req_fresh_fb)

        # 3. Now initialize WB properly
        req_valid = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_valid'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'white_balance': {'temperature': 5500, 'green': 1.0}}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=self.fake_render):
            rt.dispatch(req_valid)

        # 4. Reset adjustments in render: passing only temperature -> rejected
        req_reset = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_reset'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 1,
            'reset_adjustments': True,
            'adjustments': {'white_balance': {'temperature': 5200}}
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}):
            self.error('INVALID_REQUEST', rt.dispatch, req_reset)

        # 5. Reset adjustments in render: passing only green -> rejected
        req_reset_g = dict(req_reset)
        req_reset_g['adjustments'] = {'white_balance': {'green': 1.05}}
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}):
            self.error('INVALID_REQUEST', rt.dispatch, req_reset_g)

    def test_adjustments_patch_illegal_keys_types_ranges_rejected(self):
        sess_file = self.root / 'bounds_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '范围类型测试'
        })
        base = {'action': 'session', 'operation': 'record_feedback', 'session_path': str(sess_file), 'expected_revision': 0}

        # Unknown key
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'unknown_field': 123}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'kelvin': 5500}}})

        # Empty dict
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'tone_curve': {}}})

        # Out of bounds
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'temperature': 1499}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'temperature': 60001}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'green': 0.49}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'green': 2.01}}})

        # Bad types
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'temperature': True}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'temperature': '5000'}}})
        self.error('INVALID_REQUEST', rt.dispatch, {**base, 'adjustments': {'white_balance': {'green': float('nan')}}})

        # Tone curve array replacement and patch semantics
        # Init tone curve
        rt.dispatch({
            **base,
            'adjustments': {
                'white_balance': {'temperature': 5500, 'green': 1.0},
                'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]]}
            }
        })
        # Patch tone_curve points: replaces array as a whole while keeping mode
        res_tc = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {'tone_curve': {'points': [[0.0, 0.0], [0.3, 0.6], [1.0, 1.0]]}}
        })
        self.assertEqual(res_tc['effective_adjustments']['tone_curve']['mode'], 'Standard')
        self.assertEqual(res_tc['effective_adjustments']['tone_curve']['points'], [[0.0, 0.0], [0.3, 0.6], [1.0, 1.0]])

    def test_slow_render_cas_conflict_preserves_new_feedback_and_cross_process_barrier(self):
        import threading
        sess_file = self.root / 'barrier_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'CAS 屏障测试'
        })
        # Advance to rev 1
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'user_feedback': '初始反馈'
        })

        render_in_flight = threading.Event()
        feedback_completed = threading.Event()
        render_results = []

        def slow_fake_render(argv, **kwargs):
            # Tell the other thread that render is executing OUTSIDE lock
            render_in_flight.set()
            # Wait until feedback thread has completely modified the session
            feedback_completed.wait(timeout=5)
            return self.fake_render(argv, **kwargs)

        def run_slow_render():
            req = {
                'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_slow'), 'format': 'jpeg',
                'session_path': str(sess_file), 'expected_revision': 1,
                'adjustments': {'contrast': 25}
            }
            try:
                with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
                     patch.object(rt, 'run', side_effect=slow_fake_render):
                    res = rt.dispatch(req)
                    render_results.append(('ok', res))
            except rt.ToolError as e:
                render_results.append(('error', e.code, str(e)))

        # Launch slow render thread
        t_render = threading.Thread(target=run_slow_render)
        t_render.start()

        # Wait until render has acquired snapshot lock, released it, and started slow RT render
        self.assertTrue(render_in_flight.wait(timeout=5))

        # While render is in-flight outside lock, test that session can be loaded without blocking
        loaded = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})
        self.assertEqual(loaded['revision'], 1)

        # Now submit feedback (advances revision from 1 to 2)
        res_fb = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'change': ['客户临时改变主意：需要更冷更暗'],
            'user_feedback': '用户打断修改：不要调对比度，改成冷色调'
        })
        self.assertEqual(res_fb['revision'], 2)

        # Release the slow render
        feedback_completed.set()
        t_render.join(timeout=5)

        # Assert slow render failed with REVISION_CONFLICT
        self.assertEqual(len(render_results), 1)
        status, code, msg = render_results[0]
        self.assertEqual(status, 'error')
        self.assertEqual(code, 'REVISION_CONFLICT')

        # Verify session state: revision is 2, user feedback is preserved intact!
        sess_after = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(sess_after['revision'], 2)
        self.assertIn('不要调对比度，改成冷色调', sess_after['history'][-1]['user_feedback'])
        self.assertNotIn('contrast', sess_after['effective_adjustments'])

    def test_cross_process_slow_render_cas_barrier_preserves_new_feedback(self):
        import sys
        bin_dir = self.root / 'fake_rt_bin'
        bin_dir.mkdir(parents=True, exist_ok=True)
        started_flag = bin_dir / 'started.flag'
        continue_flag = bin_dir / 'continue.flag'
        fake_rt = bin_dir / 'mock_rt_cli.py'

        fake_script = f'''#!{sys.executable}
import sys, time
from pathlib import Path
from PIL import Image, ImageCms

args = sys.argv[1:]
if '-h' in args:
    print('RawTherapee, version 5.13')
    print('-js -q -p -c')
    sys.exit(0)

if '-o' in args:
    out_idx = args.index('-o') + 1
    out_path = Path(args[out_idx])
    b_dir = Path({repr(str(bin_dir))})
    (b_dir / 'started.flag').write_text('started')
    t0 = time.time()
    while not (b_dir / 'continue.flag').is_file():
        time.sleep(0.02)
        if time.time() - t0 > 10:
            break
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    Image.new('RGB', (32, 32), (100, 100, 100)).save(out_path, icc_profile=icc)
    sys.exit(0)
'''
        fake_rt.write_text(fake_script, encoding='utf-8')
        fake_rt.chmod(0o755)

        cli_py = Path(__file__).parent / 'rt_tool.py'
        sess_file = self.root / 'xp_barrier_sess.json'
        env = os.environ.copy()
        env['RTCLI'] = str(fake_rt)
        env['PYTHONPATH'] = str(Path(__file__).parent)

        # 1. Init session
        req_init = json.dumps({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '跨进程屏障测试'
        })
        p_init = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                                input=req_init, text=True, capture_output=True, env=env)
        self.assertEqual(p_init.returncode, 0, p_init.stderr)

        # 2. Advance to revision 1 with initial feedback
        req_fb1 = json.dumps({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'user_feedback': '反馈 1：初始色调微调'
        })
        p_fb1 = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                               input=req_fb1, text=True, capture_output=True, env=env)
        self.assertEqual(p_fb1.returncode, 0, p_fb1.stderr)

        # 3. Launch slow render CLI process on revision 1
        req_render = json.dumps({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_xp_slow'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {'contrast': 20}
        })
        p_render = subprocess.Popen([sys.executable, str(cli_py), '--request', '-'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env=env)
        p_render.stdin.write(req_render)
        p_render.stdin.close()

        # 4. Wait until the render process is in-flight executing fake RT outside lock
        t0 = time.time()
        while not started_flag.is_file():
            time.sleep(0.02)
            if time.time() - t0 > 5:
                break
        self.assertTrue(started_flag.is_file(), 'Slow render process did not reach execution stage')

        # 5. While render process is active outside lock, verify load succeeds without blocking
        req_load = json.dumps({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})
        p_load = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                                input=req_load, text=True, capture_output=True, env=env)
        self.assertEqual(p_load.returncode, 0, p_load.stderr)
        self.assertEqual(json.loads(p_load.stdout)['result']['revision'], 1)

        # 6. Submit feedback (advances revision from 1 to 2) while slow render is still executing!
        req_fb2 = json.dumps({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'change': ['跨进程插队修改：降对比度加冷色'],
            'user_feedback': '跨进程插队：用户推翻上一轮，要求更冷更暗'
        })
        p_fb2 = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                               input=req_fb2, text=True, capture_output=True, env=env)
        self.assertEqual(p_fb2.returncode, 0, p_fb2.stderr)
        self.assertEqual(json.loads(p_fb2.stdout)['result']['revision'], 2)

        # 7. Release slow render process by creating continue.flag
        continue_flag.write_text('continue')
        p_render.wait(timeout=10)
        out_render = p_render.stdout.read()
        err_render = p_render.stderr.read()
        p_render.stdout.close()
        p_render.stderr.close()

        # 8. Render process must fail CAS revision check
        self.assertEqual(p_render.returncode, 1)
        res_render = json.loads(out_render)
        self.assertFalse(res_render['ok'])
        self.assertEqual(res_render['error']['code'], 'REVISION_CONFLICT')

        # 9. Verify session on disk: revision is exactly 2, and new feedback is completely preserved!
        p_final = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                                 input=req_load, text=True, capture_output=True, env=env)
        sess_final = json.loads(p_final.stdout)['result']['session']
        self.assertEqual(sess_final['revision'], 2)
        self.assertEqual(sess_final['history'][-1]['user_feedback'], '跨进程插队：用户推翻上一轮，要求更冷更暗')
        self.assertNotIn('contrast', sess_final['effective_adjustments'])

    def test_concurrent_renders_same_revision_only_one_commits(self):
        import concurrent.futures
        sess_file = self.root / 'concurrent_renders_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '并发渲染竞争测试'
        })

        req1 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_conc1'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'contrast': 10}
        }
        req2 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_conc2'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'contrast': -10}
        }

        results = []
        def do_render(r):
            try:
                return ('ok', rt.dispatch(r))
            except rt.ToolError as e:
                return ('error', e.code)

        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(do_render, req1), executor.submit(do_render, req2)]
                for f in concurrent.futures.as_completed(futures):
                    results.append(f.result())

        statuses = [r[0] for r in results]
        self.assertIn('ok', statuses)
        self.assertIn('error', statuses)
        err = next(r for r in results if r[0] == 'error')
        self.assertEqual(err[1], 'RENDER_IN_PROGRESS')

        # Session revision must be exactly 1, and history length is exactly 1
        sess = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(sess['revision'], 1)
        self.assertEqual(len(sess['history']), 1)

    def test_manifest_hash_consistency_and_commit_failure_isolation(self):
        sess_file = self.root / 'commit_fail_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '提交故障隔离测试'
        })

        req = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_fail'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'contrast': 15}
        }

        # Simulate I/O error during atomic commit in Lock 2
        orig_atomic = rt.atomic_write_json
        def fail_atomic(path, data):
            raise OSError('Disk write failure simulation')

        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render), \
             patch.object(rt, 'atomic_write_json', side_effect=fail_atomic):
            self.error('SESSION_COMMIT_FAILED', rt.dispatch, req)

        # State does not advance!
        sess = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        self.assertEqual(sess['revision'], 0)
        self.assertEqual(len(sess['history']), 0)

    def test_clean_dryrun_and_apply_deletes_only_old_orphans(self):
        sess_file = self.root / 'clean_test_sess.json'
        out_dir = self.root / 'clean_out'
        out_dir.mkdir(parents=True, exist_ok=True)
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '清理测试'
        })
        sess_id = init_res['session_id']

        # 1. Normal committed render
        req_comm = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            res_comm = rt.dispatch(req_comm)
        comm_dir = Path(res_comm['output']).parent

        # 2. Fabricate an orphan render with matching session_id & identity, but NOT in session history
        orphan_dir = out_dir / 'orphan__rt-5.13__abc123-test'
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / '.active.lock').touch()
        (orphan_dir / 'image.jpg').write_bytes(b'fake output bytes')
        manifest_data = {
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION, 'session_id': sess_id,
            'source_sha256': rt.digest(self.source),
            'profile': {'sha256': rt.digest(self.pp3)}
        }
        (orphan_dir / 'manifest.json').write_text(json.dumps(manifest_data), encoding='utf-8')
        old_time = time.time() - 3600
        os.utime(orphan_dir, (old_time, old_time))
        os.utime(orphan_dir / 'manifest.json', (old_time, old_time))

        # 3. Dry-run clean
        clean_res = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': False, 'min_age_seconds': 1800
        })
        self.assertFalse(clean_res['apply'])
        self.assertEqual(clean_res['total_orphans'], 1)
        self.assertEqual(clean_res['orphans'][0]['path'], str(orphan_dir))
        self.assertTrue(clean_res['total_bytes'] > 0)
        self.assertTrue(orphan_dir.exists(), 'Dry-run must not delete directory')
        self.assertTrue(comm_dir.exists(), 'Committed render must not be deleted')

        # 4. Apply clean
        clean_apply_res = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 1800
        })
        self.assertTrue(clean_apply_res['apply'])
        self.assertEqual(clean_apply_res['total_orphans'], 1)
        self.assertEqual(clean_apply_res['orphans'][0]['path'], str(orphan_dir))
        self.assertFalse(orphan_dir.exists(), 'Apply clean must delete orphan directory')
        self.assertTrue(comm_dir.exists(), 'Committed render must remain intact')

    def test_clean_protective_guards_and_skip_reasons(self):
        sess_file = self.root / 'guards_sess.json'
        out_dir = self.root / 'guards_out'
        out_dir.mkdir(parents=True, exist_ok=True)
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '防护测试'
        })
        sess_id = init_res['session_id']

        # Guard 1: missing .active.lock
        d_no_lock = out_dir / 'item_no_lock'
        d_no_lock.mkdir()
        (d_no_lock / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION, 'session_id': sess_id, 'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}}))

        # Guard 2: missing manifest
        d_no_man = out_dir / 'item_no_manifest'
        d_no_man.mkdir()
        (d_no_man / '.active.lock').touch()

        # Guard 3: other session
        d_other_sess = out_dir / 'item_other_sess'
        d_other_sess.mkdir()
        (d_other_sess / '.active.lock').touch()
        (d_other_sess / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION, 'session_id': 'other_sess_xyz', 'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}}))

        # Guard 4: too young
        d_young = out_dir / 'item_young'
        d_young.mkdir()
        (d_young / '.active.lock').touch()
        (d_young / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION, 'session_id': sess_id, 'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}}))

        # Guard 5: active lock held
        d_active = out_dir / 'item_active'
        d_active.mkdir()
        (d_active / '.active.lock').touch()
        (d_active / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION, 'session_id': sess_id, 'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}}))
        old_time = time.time() - 3600
        os.utime(d_active, (old_time, old_time))
        os.utime(d_active / 'manifest.json', (old_time, old_time))
        lock_held = rt.FileLock(d_active / '.active.lock')
        lock_held.acquire()

        # Guard 6: symlink in out_dir
        real_target = self.root / 'some_real_dir'
        real_target.mkdir()
        (out_dir / 'symlink_dir').symlink_to(real_target)

        # Guard 7: legacy tool version (e.g. 0.4.0) skipped for manual review
        d_legacy = out_dir / 'item_legacy'
        d_legacy.mkdir()
        (d_legacy / '.active.lock').touch()
        (d_legacy / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'tool_version': '0.4.0', 'session_id': sess_id, 'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}}))

        try:
            res = rt.dispatch({
                'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
                'apply': True, 'min_age_seconds': 1800
            })
            self.assertEqual(res['total_orphans'], 0)
            skip_reasons = {Path(s['path']).name: s['reason'] for s in res['skipped']}
            self.assertIn('missing_or_symlink_active_lock_file', skip_reasons.get('item_no_lock', ''))
            self.assertIn('missing_manifest', skip_reasons.get('item_no_manifest', ''))
            self.assertIn('other_session_render', skip_reasons.get('item_other_sess', ''))
            self.assertIn('too_young', skip_reasons.get('item_young', ''))
            self.assertIn('active_render_in_progress', skip_reasons.get('item_active', ''))
            self.assertIn('symlink_entry_rejected', skip_reasons.get('symlink_dir', ''))
            self.assertIn('legacy_or_unknown_tool_version', skip_reasons.get('item_legacy', ''))
        finally:
            lock_held.release()

    def test_clean_corrupt_or_missing_session_aborts_without_deletion(self):
        out_dir = self.root / 'corrupt_out'
        out_dir.mkdir(parents=True, exist_ok=True)
        dummy_orphan = out_dir / 'dummy_orphan'
        dummy_orphan.mkdir()
        (dummy_orphan / 'file.txt').write_text('important data')

        # 1. Missing session file
        self.error('FILE_NOT_FOUND', rt.dispatch, {
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(self.root / 'nonexistent.json')
        })

        # 2. Corrupt session file
        bad_sess = self.root / 'bad_sess.json'
        bad_sess.write_text('{bad json syntax...')
        self.error('CORRUPT_SESSION', rt.dispatch, {
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(bad_sess)
        })

        # 3. Output dir is a symlink
        sym_out = self.root / 'sym_out'
        sym_out.symlink_to(out_dir)
        valid_sess = self.root / 'valid_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(valid_sess), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '正常会话'
        })
        self.error('SECURITY_VIOLATION', rt.dispatch, {
            'action': 'clean', 'output_dir': str(sym_out), 'session_path': str(valid_sess)
        })

        # 4. Output dir is root or home
        self.error('SECURITY_VIOLATION', rt.dispatch, {
            'action': 'clean', 'output_dir': '/', 'session_path': str(valid_sess)
        })
        self.error('SECURITY_VIOLATION', rt.dispatch, {
            'action': 'clean', 'output_dir': str(Path.home()), 'session_path': str(valid_sess)
        })

        # Ensure dummy_orphan is completely untouched!
        self.assertTrue((dummy_orphan / 'file.txt').is_file())

    def test_clean_lifecycle_cross_process_pause_and_failure_disposal(self):
        import threading
        sess_file = self.root / 'pause_sess.json'
        out_dir = self.root / 'pause_out'
        out_dir.mkdir(parents=True, exist_ok=True)
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '生命周期屏障测试'
        })

        manifest_written_event = threading.Event()
        continue_commit_event = threading.Event()
        captured_folder = []

        def pausing_fake_render(argv, **kwargs):
            res = self.fake_render(argv, **kwargs)
            out_file = Path(argv[argv.index('-o') + 1])
            captured_folder.append(out_file.parent.parent)
            return res

        render_thread_done = threading.Event()
        t = None
        orig_filelock_enter = rt.FileLock.__enter__
        def hooked_filelock_enter(lock_self):
            if t is not None and threading.current_thread() == t and lock_self.lock_path.name == Path(str(sess_file) + '.lock').name:
                if captured_folder:
                    manifest_written_event.set()
                    continue_commit_event.wait(timeout=10)
            return orig_filelock_enter(lock_self)

        req_render = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }

        render_thread_done = threading.Event()
        def run_render():
            with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
                 patch.object(rt, 'run', side_effect=pausing_fake_render), \
                 patch.object(rt.FileLock, '__enter__', side_effect=hooked_filelock_enter, autospec=True):
                rt.dispatch(req_render)
            render_thread_done.set()

        t = threading.Thread(target=run_render)
        t.start()

        # 1. Wait until manifest is written on disk and render is paused BEFORE CAS commit
        self.assertTrue(manifest_written_event.wait(timeout=5))
        f_dir = captured_folder[0]
        self.assertTrue((f_dir / 'manifest.json').is_file(), 'Manifest must be written')

        # 2. While render is holding .active.lock, run GC with apply: true, min_age: 0
        gc_during = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 0
        })
        self.assertEqual(gc_during['total_orphans'], 0)
        self.assertTrue(f_dir.exists(), 'GC apply must NOT delete folder while active lock is held!')
        skip_during = {Path(s['path']).name: s['reason'] for s in gc_during['skipped']}
        self.assertIn('active_render_in_progress', skip_during.get(f_dir.name, ''))

        # 3. Resume render to commit to session
        continue_commit_event.set()
        t.join(timeout=5)
        self.assertTrue(render_thread_done.is_set())

        # 4. Now render has completed and released .active.lock. Run GC apply again!
        gc_after_commit = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 0
        })
        self.assertEqual(gc_after_commit['total_orphans'], 0)
        self.assertTrue(f_dir.exists(), 'Committed folder must NOT be deleted by GC!')
        skip_after = {Path(s['path']).name: s['reason'] for s in gc_after_commit['skipped']}
        self.assertIn('committed_to_session_history', skip_after.get(f_dir.name, ''))

        # 5. Now test failed render: simulate CAS conflict
        req_fail = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}):
            self.error('REVISION_CONFLICT', rt.dispatch, req_fail)

        # Uncommitted orphan folder left behind
        fail_dir = out_dir / 'fail_orphan__rt-5.13__abc-999'
        fail_dir.mkdir(parents=True, exist_ok=True)
        (fail_dir / '.active.lock').touch()
        (fail_dir / 'image.jpg').write_bytes(b'output bytes')
        (fail_dir / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION, 'session_id': init_res['session_id'],
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # Since lock is released, GC apply with min_age=0 must delete it!
        gc_clean_fail = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 0
        })
        self.assertEqual(gc_clean_fail['total_orphans'], 1)
        self.assertEqual(gc_clean_fail['orphans'][0]['path'], str(fail_dir))
        self.assertFalse(fail_dir.exists(), 'Failed/orphan render must be cleaned after lock release!')
        self.assertTrue(f_dir.exists(), 'Committed render must remain intact!')

    def test_reset_fields_and_adjustments_mutual_exclusion(self):
        sess_file = self.root / 'mutex_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '互斥测试'
        })
        # Render: both reset_adjustments and reset_fields -> INVALID_REQUEST
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_mutex'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_adjustments': True, 'reset_fields': ['contrast']
        })
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_mutex'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_adjustments': False, 'reset_fields': ['contrast']
        })
        # Record feedback: both reset_adjustments and reset_fields -> INVALID_REQUEST
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_adjustments': True, 'reset_fields': ['contrast']
        })
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_adjustments': False, 'reset_fields': ['contrast']
        })

    def test_reset_fields_whitelist_syntax_validation(self):
        sess_file = self.root / 'syntax_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '语法校验'
        })
        # Empty array
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_fields': []
        })
        # Duplicates
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_fields': ['contrast', 'contrast']
        })
        # Dot path
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_fields': ['white_balance.temperature']
        })
        # Unknown field
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'reset_fields': ['non_existent_category']
        })
        # Session init/load with reset parameters rejected
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'load',
            'session_path': str(sess_file),
            'reset_fields': ['contrast']
        })

    def test_single_item_reset_wb_preserves_tone_curve_and_sharpen(self):
        sess_file = self.root / 'wb_preserve_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '单项重置测试'
        })
        # Revision 0 -> 1: record WB, tone_curve, and sharpen
        fb1 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'white_balance': {'temperature': 5200, 'green': 1.05},
                'tone_curve': {'mode': 'Standard', 'points': [[0.0, 0.0], [1.0, 1.0]]},
                'sharpen': {'enabled': True, 'amount': 150, 'radius': 1.2}
            }
        })
        self.assertEqual(fb1['revision'], 1)
        self.assertIn('white_balance', fb1['effective_adjustments'])
        self.assertIn('tone_curve', fb1['effective_adjustments'])
        self.assertIn('sharpen', fb1['effective_adjustments'])

        # Revision 1 -> 2: render with reset_fields: ['white_balance']
        req_render = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_wb_reset'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 1,
            'reset_fields': ['white_balance']
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            res = rt.dispatch(req_render)

        self.assertEqual(res['session_revision'], 2)
        sess = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_file)})['session']
        eff = sess['effective_adjustments']
        self.assertNotIn('white_balance', eff, 'white_balance must be deleted')
        self.assertIn('tone_curve', eff, 'tone_curve must be preserved')
        self.assertIn('sharpen', eff, 'sharpen must be preserved')

        # Check effective.pp3 written to output: must not contain White Balance patch
        eff_pp3 = (Path(res['output']).parent / 'effective.pp3').read_text()
        self.assertNotIn('[White Balance]', eff_pp3, 'PP3 must not have White Balance patch, falls back to base profile')
        self.assertIn('[Sharpening]', eff_pp3, 'PP3 must retain Sharpening patch')

    def test_wb_reset_with_incomplete_wb_patch_rejected(self):
        sess_file = self.root / 'wb_incomp_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '不完整补丁测试'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'white_balance': {'temperature': 5200, 'green': 1.05}}
        })
        # Reset WB, but pass incomplete WB patch (missing green) -> INVALID_REQUEST
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'reset_fields': ['white_balance'],
            'adjustments': {'white_balance': {'temperature': 6000}}
        })

    def test_wb_reset_with_new_complete_wb_patch_succeeds(self):
        sess_file = self.root / 'wb_comp_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '完整补丁测试'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'white_balance': {'temperature': 5200, 'green': 1.05},
                'contrast': 20
            }
        })
        fb = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'reset_fields': ['white_balance'],
            'adjustments': {'white_balance': {'temperature': 6500, 'green': 1.15}}
        })
        self.assertEqual(fb['revision'], 2)
        self.assertEqual(fb['effective_adjustments']['white_balance']['temperature'], 6500)
        self.assertEqual(fb['effective_adjustments']['white_balance']['green'], 1.15)
        self.assertEqual(fb['effective_adjustments']['contrast'], 20)

    def test_render_gate_engine_count_one_and_no_output_dir(self):
        import threading
        sess_file = self.root / 'gate_barrier_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '防重入测试'
        })
        out_dir1 = self.root / 'gate_out1'
        out_dir2 = self.root / 'gate_out2'
        req1 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir1), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }
        req2 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir2), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }

        engine_calls = []
        barrier_in_engine = threading.Event()
        hold_in_engine = threading.Event()

        def slow_fake_render(argv, timeout=300, settings=None):
            engine_calls.append(argv)
            self.fake_render(argv, timeout=timeout, settings=settings)
            barrier_in_engine.set()
            hold_in_engine.wait(timeout=5)
            return 'fake slow render ok'

        def run_thread(req, res_list):
            try:
                res_list.append(('ok', rt.dispatch(req)))
            except rt.ToolError as e:
                res_list.append(('error', e.code))

        results1 = []
        results2 = []
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=slow_fake_render):
            t1 = threading.Thread(target=run_thread, args=(req1, results1))
            t1.start()
            barrier_in_engine.wait(timeout=5)
            # Render 1 is now actively inside engine holding render_gate!
            # Render 2 attempts to run on the SAME session:
            t2 = threading.Thread(target=run_thread, args=(req2, results2))
            t2.start()
            t2.join(timeout=5)

            # Release Render 1
            hold_in_engine.set()
            t1.join(timeout=5)

        self.assertEqual(len(results1), 1)
        self.assertEqual(results1[0][0], 'ok')
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0][0], 'error')
        self.assertEqual(results2[0][1], 'RENDER_IN_PROGRESS')

        # Competing engine call count must be EXACTLY 1!
        self.assertEqual(len(engine_calls), 1)

        # Competitor out_dir2 must have NO created directories!
        if out_dir2.exists():
            self.assertEqual(len(list(out_dir2.iterdir())), 0)

    def test_render_gate_feedback_non_blocking_and_late_cas_conflict(self):
        import threading
        sess_file = self.root / 'feedback_conflict_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '晚CAS冲突测试'
        })
        out_dir = self.root / 'out_cas_conflict'
        req_render = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }

        barrier_in_engine = threading.Event()
        hold_in_engine = threading.Event()

        def slow_fake_render(argv, timeout=300, settings=None):
            self.fake_render(argv, timeout=timeout, settings=settings)
            barrier_in_engine.set()
            hold_in_engine.wait(timeout=5)
            return 'fake slow render ok'

        render_res = []
        def do_render():
            try:
                render_res.append(('ok', rt.dispatch(req_render)))
            except rt.ToolError as e:
                render_res.append(('error', e.code))

        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=slow_fake_render):
            t = threading.Thread(target=do_render)
            t.start()
            barrier_in_engine.wait(timeout=5)

            # While render is holding gate, record_feedback must NOT block and advance revision!
            fb = rt.dispatch({
                'action': 'session', 'operation': 'record_feedback',
                'session_path': str(sess_file), 'expected_revision': 0,
                'user_feedback': '渲染中途插入反馈'
            })
            self.assertEqual(fb['revision'], 1)

            # Let render finish engine and attempt CAS:
            hold_in_engine.set()
            t.join(timeout=5)

        self.assertEqual(len(render_res), 1)
        self.assertEqual(render_res[0][0], 'error')
        self.assertEqual(render_res[0][1], 'REVISION_CONFLICT')

        # The output folder must be preserved as an auditable orphan!
        out_subdirs = list(out_dir.iterdir())
        self.assertEqual(len(out_subdirs), 1)
        orphan_dir = out_subdirs[0]
        self.assertTrue((orphan_dir / 'manifest.json').is_file(), 'Manifest must exist for audit')
        self.assertTrue((orphan_dir / 'image.jpg').is_file(), 'Output image must exist for audit')

    def test_render_gate_reusable_after_success_error_and_process_exit(self):
        sess_file = self.root / 'reusable_gate_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'Gate复用测试'
        })
        out_dir = self.root / 'reusable_out'

        # 1. Success -> Gate reusable
        req1 = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            res1 = rt.dispatch(req1)
            self.assertEqual(res1['session_revision'], 1)

            # Next render can acquire gate immediately:
            req2 = dict(req1, expected_revision=1)
            res2 = rt.dispatch(req2)
            self.assertEqual(res2['session_revision'], 2)

        # 2. Exception in engine -> Gate reusable
        def failing_render(*args, **kwargs):
            raise OSError('Engine crashed')

        req3 = dict(req1, expected_revision=2)
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=failing_render):
            with self.assertRaises(OSError):
                rt.dispatch(req3)

        # Gate must be free after exception:
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            res4 = rt.dispatch(req3)
            self.assertEqual(res4['session_revision'], 3)

        # 3. Process exit simulation: subprocess holds gate then exits abruptly
        import subprocess, sys
        gate_path = Path(str(sess_file) + '.render.gate')

        child_code = (
            "import os, fcntl, time, sys\n"
            f"fd = os.open({str(gate_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "sys.stdout.write('LOCKED\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        p = subprocess.Popen([sys.executable, '-c', child_code], stdout=subprocess.PIPE, text=True)
        line = p.stdout.readline()
        p.stdout.close()
        self.assertEqual(line.strip(), 'LOCKED')

        # While child process holds gate:
        child_gate_lock = rt.FileLock(gate_path)
        self.assertFalse(child_gate_lock.acquire(non_blocking=True))

        # Terminate child process abruptly
        p.kill()
        p.wait()

        # Now OS has released flock, main process can acquire gate:
        self.assertTrue(child_gate_lock.acquire(non_blocking=True))
        child_gate_lock.release()

        # Gate file must NOT be unlinked
        self.assertTrue(gate_path.exists())

    def test_render_gate_parallel_for_different_sessions(self):
        import threading
        sess_a = self.root / 'sess_a.json'
        sess_b = self.root / 'sess_b.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_a), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '会话A'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_b), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '会话B'
        })
        out_a = self.root / 'out_a'
        out_b = self.root / 'out_b'

        both_in_engine = threading.Barrier(2)

        def parallel_fake_render(argv, timeout=300, settings=None):
            self.fake_render(argv, timeout=timeout, settings=settings)
            # Both threads must reach this point concurrently!
            both_in_engine.wait(timeout=5)
            return 'parallel render ok'

        res_a = []
        res_b = []
        req_a = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_a), 'format': 'jpeg',
            'session_path': str(sess_a), 'expected_revision': 0
        }
        req_b = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_b), 'format': 'jpeg',
            'session_path': str(sess_b), 'expected_revision': 0
        }

        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=parallel_fake_render):
            ta = threading.Thread(target=lambda: res_a.append(rt.dispatch(req_a)))
            tb = threading.Thread(target=lambda: res_b.append(rt.dispatch(req_b)))
            ta.start()
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

        self.assertEqual(len(res_a), 1)
        self.assertEqual(res_a[0]['session_revision'], 1)
        self.assertEqual(len(res_b), 1)
        self.assertEqual(res_b[0]['session_revision'], 1)

    def test_clean_manifest_generated_by_render_has_protocol_1(self):
        sess_file = self.root / 'proto1_render_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '协议字段测试'
        })
        out_dir = self.root / 'proto1_out'
        req = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0
        }
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            res = rt.dispatch(req)
        manifest_path = Path(res['manifest'])
        m_data = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertIn('cleanup_protocol_version', m_data)
        self.assertIs(type(m_data['cleanup_protocol_version']), int)
        self.assertEqual(m_data['cleanup_protocol_version'], 1)

    def test_cleanup_protocol_version_branches_and_fallbacks(self):
        sess_file = self.root / 'branches_sess.json'
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '协议分支测试'
        })
        sess_id = init_res['session_id']
        out_dir = self.root / 'branches_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        def make_orphan(name, man_extra):
            d = out_dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / '.active.lock').touch()
            (d / 'image.jpg').write_bytes(b'output')
            m = {
                'schema_version': 1,
                'tool_version': '0.4.1',
                'session_id': sess_id,
                'source_sha256': rt.digest(self.source),
                'profile': {'sha256': rt.digest(self.pp3)}
            }
            m.update(man_extra)
            (d / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
            return d

        # 1. Known 0.4.1 legacy without cleanup_protocol_version -> can clean
        d_legacy_041 = make_orphan('orphan_041', {})
        # 2. Protocol 1 with current version 0.4.2 -> can clean
        d_proto1_042 = make_orphan('orphan_proto1_042', {'cleanup_protocol_version': 1, 'tool_version': '0.4.2'})
        # 3. Protocol 1 with simulated future version 0.5.0 -> can clean
        d_proto1_050 = make_orphan('orphan_proto1_050', {'cleanup_protocol_version': 1, 'tool_version': '0.5.0'})
        # 4. Unknown protocol version 2 -> reject, no fallback
        d_proto2 = make_orphan('orphan_proto2', {'cleanup_protocol_version': 2})
        # 5. Boolean protocol version True -> reject, no fallback
        d_protobool = make_orphan('orphan_protobool', {'cleanup_protocol_version': True})
        # 6. Legacy 0.4.0 without cleanup_protocol_version -> reject
        d_legacy_040 = make_orphan('orphan_040', {'tool_version': '0.4.0'})
        # 7. Unsupported schema_version (e.g. 2 or True)
        d_bad_schema = make_orphan('orphan_bad_schema', {'schema_version': 2, 'cleanup_protocol_version': 1})
        d_bool_schema = make_orphan('orphan_bool_schema', {'schema_version': True, 'cleanup_protocol_version': 1})

        res = rt.dispatch({
            'action': 'clean',
            'session_path': str(sess_file),
            'output_dir': str(out_dir),
            'apply': True,
            'min_age_seconds': 0
        })

        reclaimed_paths = {o['path'] for o in res['orphans']}
        self.assertIn(str(d_legacy_041), reclaimed_paths)
        self.assertIn(str(d_proto1_042), reclaimed_paths)
        self.assertIn(str(d_proto1_050), reclaimed_paths)
        self.assertEqual(len(reclaimed_paths), 3)

        skip_reasons = {Path(s['path']).name: s['reason'] for s in res['skipped']}
        self.assertIn('unsupported_cleanup_protocol', skip_reasons.get(d_proto2.name, ''))
        self.assertIn('unsupported_cleanup_protocol', skip_reasons.get(d_protobool.name, ''))
        self.assertIn('legacy_or_unknown_tool_version', skip_reasons.get(d_legacy_040.name, ''))
        self.assertIn('unsupported_manifest_schema', skip_reasons.get(d_bad_schema.name, ''))
        self.assertIn('unsupported_manifest_schema', skip_reasons.get(d_bool_schema.name, ''))

    def test_clean_param_ambiguity_and_duplicate_session_id_rejection(self):
        sess_a = self.root / 'ambig_a.json'
        sess_b = self.root / 'ambig_b.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_a), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '歧义A', 'session_id': 'ambig_sess_a'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_b), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '歧义B', 'session_id': 'ambig_sess_b'
        })
        out_dir = self.root / 'ambig_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Both session_path and session_paths -> INVALID_REQUEST
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'clean',
            'session_path': str(sess_a),
            'session_paths': [str(sess_b)],
            'output_dir': str(out_dir)
        })

        # 2. Neither session_path nor session_paths -> INVALID_REQUEST
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'clean',
            'output_dir': str(out_dir)
        })

        # 3. session_paths empty -> INVALID_REQUEST
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'clean',
            'session_paths': [],
            'output_dir': str(out_dir)
        })

        # 4. Distinct session files sharing duplicate session_id -> INVALID_REQUEST
        sess_dup_id = self.root / 'dup_id.json'
        a_data = json.loads(sess_a.read_text(encoding='utf-8'))
        sess_dup_id.write_text(json.dumps(a_data), encoding='utf-8')  # exact same session_id in distinct file!
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'clean',
            'session_paths': [str(sess_a), str(sess_dup_id)],
            'output_dir': str(out_dir)
        })

    def test_multi_session_clean_mixed_committed_and_orphans(self):
        sess_a = self.root / 'multi_a.json'
        sess_b = self.root / 'multi_b.json'
        sess_c = self.root / 'multi_c.json'
        init_a = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_a), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '多会话A', 'session_id': 'multi_sess_a'
        })
        init_b = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_b), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '多会话B', 'session_id': 'multi_sess_b'
        })
        init_c = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_c), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '多会话C', 'session_id': 'multi_sess_c'
        })

        out_dir = self.root / 'multi_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        # Committed render for Session A
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), \
             patch.object(rt, 'run', side_effect=self.fake_render):
            res_comm_a = rt.dispatch({
                'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(out_dir), 'format': 'jpeg',
                'session_path': str(sess_a), 'expected_revision': 0
            })
            res_comm_b = rt.dispatch({
                'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(out_dir), 'format': 'jpeg',
                'session_path': str(sess_b), 'expected_revision': 0
            })
        dir_comm_a = Path(res_comm_a['output']).parent
        dir_comm_b = Path(res_comm_b['output']).parent

        def make_orphan(name, sess_id):
            d = out_dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / '.active.lock').touch()
            (d / 'image.jpg').write_bytes(b'output')
            m = {
                'schema_version': 1,
                'cleanup_protocol_version': 1,
                'tool_version': '0.4.2',
                'session_id': sess_id,
                'source_sha256': rt.digest(self.source),
                'profile': {'sha256': rt.digest(self.pp3)}
            }
            (d / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
            return d

        dir_orph_a = make_orphan('orph_a', init_a['session_id'])
        dir_orph_b = make_orphan('orph_b', init_b['session_id'])
        dir_orph_c = make_orphan('orph_c', init_c['session_id'])

        # Multi-session clean for A and B only
        res = rt.dispatch({
            'action': 'clean',
            'session_paths': [str(sess_a), str(sess_b)],
            'output_dir': str(out_dir),
            'apply': True,
            'min_age_seconds': 0
        })

        reclaimed = {o['path'] for o in res['orphans']}
        self.assertEqual(len(reclaimed), 2)
        self.assertIn(str(dir_orph_a), reclaimed)
        self.assertIn(str(dir_orph_b), reclaimed)

        # Committed renders for both A and B MUST remain intact!
        self.assertTrue(dir_comm_a.exists(), 'Committed render for Session A must not be deleted!')
        self.assertTrue(dir_comm_b.exists(), 'Committed render for Session B must not be deleted!')
        # Session C orphan MUST remain intact (not in session_paths)!
        self.assertTrue(dir_orph_c.exists(), 'Unrelated Session C orphan must not be deleted!')

        # Deleted orphans must no longer exist
        self.assertFalse(dir_orph_a.exists())
        self.assertFalse(dir_orph_b.exists())

    def test_multi_session_clean_corrupt_session_fail_closed_zero_deletion(self):
        sess_good = self.root / 'good_sess.json'
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_good), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '正常会话'
        })
        out_dir = self.root / 'fail_closed_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        orphan_dir = out_dir / 'real_orphan'
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / '.active.lock').touch()
        (orphan_dir / 'image.jpg').write_bytes(b'output')
        (orphan_dir / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': '0.4.2',
            'session_id': init_res['session_id'],
            'source_sha256': rt.digest(self.source),
            'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        corrupt_sess = self.root / 'corrupt_session.json'
        corrupt_sess.write_text('{bad json syntax...')

        # Call with both good and corrupt session:
        self.error('CORRUPT_SESSION', rt.dispatch, {
            'action': 'clean',
            'session_paths': [str(sess_good), str(corrupt_sess)],
            'output_dir': str(out_dir),
            'apply': True,
            'min_age_seconds': 0
        })

        # Fail-closed guarantee: zero deletion!
        self.assertTrue(orphan_dir.exists(), 'Orphan must remain untouched when any session is corrupt!')

    def test_clean_fresh_read_protects_concurrent_commit(self):
        sess_file = self.root / 'fresh_read_sess.json'
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '并发提交防护'
        })
        out_dir = self.root / 'fresh_read_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        cand_dir = out_dir / 'cand_orphan'
        cand_dir.mkdir(parents=True, exist_ok=True)
        (cand_dir / '.active.lock').touch()
        out_img = cand_dir / 'image.jpg'
        out_img.write_bytes(b'output')
        man_file = cand_dir / 'manifest.json'
        man_file.write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': '0.4.2',
            'session_id': init_res['session_id'],
            'source_sha256': rt.digest(self.source),
            'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # While clean is about to delete, simulate concurrent commit into session history
        s_data = json.loads(sess_file.read_text(encoding='utf-8'))
        s_data['history'].append({
            'revision': 0, 'committed_revision': 1,
            'status': 'SUCCESS',
            'output_path': str(out_img),
            'manifest_path': str(man_file)
        })
        sess_file.write_text(json.dumps(s_data), encoding='utf-8')

        # Clean runs with apply: True
        res = rt.dispatch({
            'action': 'clean',
            'session_path': str(sess_file),
            'output_dir': str(out_dir),
            'apply': True,
            'min_age_seconds': 0
        })

        self.assertEqual(res['total_orphans'], 0)
        self.assertTrue(cand_dir.exists(), 'Fresh read must protect newly committed directory!')
        skip_reasons = {Path(s['path']).name: s['reason'] for s in res['skipped']}
        self.assertIn('committed_to_session_history', skip_reasons.get(cand_dir.name, ''))

    def test_clean_rejects_duplicate_paths_in_session_paths(self):
        sess_file = self.root / 'dup_test_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': '重复路径拒绝测试'
        })
        out_dir = self.root / 'dup_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Exact string duplicate rejected by schema (uniqueItems)
        req_dup_schema = {
            'action': 'clean',
            'session_paths': [str(sess_file), str(sess_file)],
            'output_dir': str(out_dir),
            'apply': False
        }
        self.error('INVALID_REQUEST', rt.dispatch, req_dup_schema)

        # 2. Distinct string representation pointing to same canonical file rejected by runtime
        dup_canon_path = str(sess_file.parent) + '/./' + sess_file.name
        req_dup_runtime = {
            'action': 'clean',
            'session_paths': [str(sess_file), dup_canon_path],
            'output_dir': str(out_dir),
            'apply': False
        }
        try:
            rt.dispatch(req_dup_runtime)
            self.fail('Runtime canonical duplicate must raise ToolError')
        except rt.ToolError as e:
            self.assertEqual(e.code, 'INVALID_REQUEST')
            self.assertIn('Duplicate session path', str(e))

    def test_clean_fresh_read_aborts_on_corrupt_or_replaced_session(self):
        sess1 = self.root / 'fresh_sess1.json'
        sess2 = self.root / 'fresh_sess2.json'
        init1 = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess1), 'source': str(self.source), 'base_profile': str(self.pp3),
            'session_id': 'sess_fresh_test_1',
            'confirmed_brief': '新鲜读取破坏防护1'
        })
        init2 = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess2), 'source': str(self.source), 'base_profile': str(self.pp3),
            'session_id': 'sess_fresh_test_2',
            'confirmed_brief': '新鲜读取破坏防护2'
        })

        out_dir = self.root / 'fresh_abort_out'
        out_dir.mkdir(parents=True, exist_ok=True)

        orphan_dir = out_dir / 'orphan_cand'
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / '.active.lock').touch()
        (orphan_dir / 'image.jpg').write_bytes(b'orphan bytes')
        (orphan_dir / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': '0.4.2',
            'session_id': init1['session_id'],
            'source_sha256': rt.digest(self.source),
            'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # Scenario A: session 2 is corrupt JSON when fresh-read happens during deletion evaluation
        # We can hook into FileLock or corrupt the file directly right before deletion loop
        # Wait, clean runs sequentially: pre-check passes, then candidates are gathered, then right before deletion fresh-read is done.
        # Let's corrupt sess2 after pre-check by mocking or using a small wrapper:
        orig_iterdir = Path.iterdir
        def corrupt_before_listing(path_self):
            if path_self == out_dir:
                # Corrupt sess2 right as output_dir is being listed (between pre-check and fresh read)
                sess2.write_text('{invalid_json: corrupt', encoding='utf-8')
            return orig_iterdir(path_self)

        with patch.object(Path, 'iterdir', corrupt_before_listing):
            self.error('CORRUPT_SESSION', rt.dispatch, {
                'action': 'clean',
                'session_paths': [str(sess1), str(sess2)],
                'output_dir': str(out_dir),
                'apply': True,
                'min_age_seconds': 0
            })

        # Zero deletion guarantee!
        self.assertTrue(orphan_dir.exists(), 'Orphan must remain untouched when fresh-read encounters corruption!')

        # Scenario B: session 2 session_id is replaced / tampered between pre-check and fresh-read
        # Restore sess2 to valid structure with tampered session_id
        sess2_data = json.loads(sess1.read_text(encoding='utf-8'))
        sess2_data['session_id'] = 'tampered_session_id_999'
        sess2.write_text(json.dumps(sess2_data), encoding='utf-8')

        def tamper_before_listing(path_self):
            if path_self == out_dir:
                s = json.loads(sess2.read_text(encoding='utf-8'))
                s['session_id'] = 'another_replaced_id_888'
                sess2.write_text(json.dumps(s), encoding='utf-8')
            return orig_iterdir(path_self)

        with patch.object(Path, 'iterdir', tamper_before_listing):
            self.error('SESSION_ID_MISMATCH', rt.dispatch, {
                'action': 'clean',
                'session_paths': [str(sess1), str(sess2)],
                'output_dir': str(out_dir),
                'apply': True,
                'min_age_seconds': 0
            })

        self.assertTrue(orphan_dir.exists(), 'Orphan must remain untouched when session identity changes!')

    def test_two_independent_processes_actual_render_entrance_competition(self):
        fake_rt = self.root / 'fake_rt_proc_gate.py'
        barrier_dir = self.root / 'proc_barrier'
        barrier_dir.mkdir(parents=True, exist_ok=True)
        p1_started = barrier_dir / 'p1_started.flag'
        p1_release = barrier_dir / 'p1_release.flag'
        engine_calls_log = barrier_dir / 'engine_calls.txt'

        fake_script = f'''#!{sys.executable}
import sys, os, time, pathlib
from PIL import Image, ImageCms

args = sys.argv[1:]
if '-h' in args:
    print('RawTherapee, version 5.13')
    print('-js -q -p -c')
    sys.exit(0)

if '-o' in args:
    log_file = pathlib.Path({repr(str(engine_calls_log))})
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write('called\\n')

    flag = pathlib.Path({repr(str(p1_started))})
    flag.write_text('started', encoding='utf-8')

    rel = pathlib.Path({repr(str(p1_release))})
    t0 = time.time()
    while not rel.is_file():
        time.sleep(0.02)
        if time.time() - t0 > 10:
            break

    out_idx = args.index('-o') + 1
    out_path = args[out_idx]
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    Image.new('RGB', (32, 32), (120, 120, 120)).save(out_path, icc_profile=icc)
    sys.exit(0)

sys.exit(0)
'''
        fake_rt.write_text(fake_script, encoding='utf-8')
        fake_rt.chmod(0o755)

        cli_py = Path(__file__).parent / 'rt_tool.py'
        sess_file = self.root / 'proc_gate_sess.json'
        env = os.environ.copy()
        env['RTCLI'] = str(fake_rt)
        env['PYTHONPATH'] = str(Path(__file__).parent)

        # 1. Init session via actual entry point
        p_init = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                                input=json.dumps({
                                    'action': 'session', 'operation': 'init',
                                    'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
                                    'confirmed_brief': '实际入口双进程竞争验收'
                                }),
                                text=True, capture_output=True, env=env)
        self.assertEqual(p_init.returncode, 0, p_init.stderr)

        out_dir1 = self.root / 'out_proc1'
        out_dir2 = self.root / 'out_proc2'

        # 2. Process 1: Start render on revision 0 via actual entry point
        req1 = json.dumps({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir1), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'contrast': 10}
        })
        p1 = subprocess.Popen([sys.executable, str(cli_py), '--request', '-'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env)
        p1.stdin.write(req1)
        p1.stdin.close()

        # 3. Wait until Process 1 has acquired .render.gate and reached engine execution
        t0 = time.time()
        while not p1_started.is_file():
            time.sleep(0.02)
            if time.time() - t0 > 5:
                break
        self.assertTrue(p1_started.is_file(), 'Process 1 did not reach engine inside render gate')

        # 4. Process 2: Simultaneously invoke actual entry point for the same session!
        req2 = json.dumps({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir2), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {'saturation': 15}
        })
        p2 = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                            input=req2, text=True, capture_output=True, env=env)

        # 5. Process 2 must be immediately rejected with RENDER_IN_PROGRESS!
        self.assertEqual(p2.returncode, 1)
        res2 = json.loads(p2.stdout)
        self.assertFalse(res2['ok'])
        self.assertEqual(res2['error']['code'], 'RENDER_IN_PROGRESS')
        self.assertIn('A render is already in progress', res2['error']['message'])

        # Engine was NOT called by Process 2! Log must contain exactly 1 call from Process 1
        calls_during_p2 = engine_calls_log.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(calls_during_p2), 1, 'Process 2 must never enter the engine!')

        # Output directory for Process 2 must NOT have been created / no render output folders
        if out_dir2.exists():
            self.assertEqual(list(out_dir2.iterdir()), [], 'Process 2 must not produce output directories!')

        # 6. Now release Process 1
        p1_release.write_text('release', encoding='utf-8')
        p1.wait(timeout=10)
        stdout1 = p1.stdout.read()
        stderr1 = p1.stderr.read()
        p1.stdout.close()
        p1.stderr.close()
        self.assertEqual(p1.returncode, 0, stderr1)
        res1 = json.loads(stdout1)
        self.assertTrue(res1['ok'])
        self.assertEqual(res1['result']['session_revision'], 1)

        # 7. Verify session history on disk committed by Process 1
        sess_now = json.loads(sess_file.read_text(encoding='utf-8'))
        self.assertEqual(sess_now['revision'], 1)
        self.assertEqual(len(sess_now['history']), 1)
        self.assertEqual(sess_now['history'][0]['adjustments'], {'contrast': 10})

        # Total engine calls remains exactly 1!
        all_calls = engine_calls_log.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(all_calls), 1)

        # 8. Verify render gate is freed and reusable: Process 3 can now render revision 1
        out_dir3 = self.root / 'out_proc3'
        req3 = json.dumps({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(out_dir3), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {'contrast': 20}
        })
        p3 = subprocess.run([sys.executable, str(cli_py), '--request', '-'],
                            input=req3, text=True, capture_output=True, env=env)
        self.assertEqual(p3.returncode, 0, p3.stderr)
        res3 = json.loads(p3.stdout)
        self.assertTrue(res3['ok'])
        self.assertEqual(res3['result']['session_revision'], 2)

        # Total engine calls is now 2
        p3_calls = engine_calls_log.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(p3_calls), 2)

    def test_phase1_new_adjustments_pp3_mapping(self):
        """Verify exact RT 5.13 PP3 section and key names for new adjustments.
        NOTE: Uses mock engine / synthetic placeholder NEF fixture, not real NEF quality or decoding verification.
        """
        # 1. highlight_recovery & highlight_compression
        req = dict(self.request)
        req['adjustments'] = {
            'highlight_recovery': {'enabled': True, 'method': 'cielab'},
            'highlight_compression': {'amount': 250, 'threshold': 40}
        }
        res = self.render_dispatch(req)
        eff_pp3 = Path(res['output']).parent / 'effective.pp3'
        text = eff_pp3.read_text(encoding='utf-8')
        self.assertIn('[HLRecovery]', text)
        self.assertIn('Enabled=true', text)
        self.assertIn('Method=CIELab blending', text)
        self.assertIn('[Exposure]', text)
        self.assertIn('HighlightCompr=250', text)
        self.assertIn('HighlightComprThreshold=40', text)

        # 2. All 5 highlight_recovery methods
        methods_map = {
            'luminance': 'Luminance',
            'cielab': 'CIELab blending',
            'blend': 'Blend',
            'color': 'Color',
            'coloropp': 'Coloropp'
        }
        for m_in, m_expected in methods_map.items():
            req_m = dict(self.request)
            req_m['adjustments'] = {'highlight_recovery': {'enabled': True, 'method': m_in}}
            res_m = self.render_dispatch(req_m)
            m_pp3 = Path(res_m['output']).parent / 'effective.pp3'
            m_text = m_pp3.read_text(encoding='utf-8')
            self.assertIn(f'Method={m_expected}', m_text)

        # 3. demosaic and dual demosaic controls
        req_dm = dict(self.request)
        req_dm['adjustments'] = {
            'demosaic': {
                'method': 'amazevng4',
                'dual_contrast_auto': False,
                'dual_contrast': 35.5
            }
        }
        res_dm = self.render_dispatch(req_dm)
        dm_text = (Path(res_dm['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
        self.assertIn('[RAW Bayer]', dm_text)
        self.assertIn('Method=amazevng4', dm_text)
        self.assertIn('DualDemosaicAutoContrast=false', dm_text)
        self.assertIn('DualDemosaicContrast=35.5', dm_text)

        # 4. raw_preprocess (both [RAW] and [RAW Bayer])
        req_rp = dict(self.request)
        req_rp['adjustments'] = {
            'raw_preprocess': {
                'hot_pixel_filter': True,
                'dead_pixel_filter': True,
                'hot_dead_pixel_threshold': 80,
                'line_noise': 120,
                'line_noise_direction': 'vertical',
                'green_equilibration': 15
            }
        }
        res_rp = self.render_dispatch(req_rp)
        rp_text = (Path(res_rp['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
        self.assertIn('[RAW]', rp_text)
        self.assertIn('HotPixelFilter=true', rp_text)
        self.assertIn('DeadPixelFilter=true', rp_text)
        self.assertIn('HotDeadPixelThresh=80', rp_text)
        self.assertIn('[RAW Bayer]', rp_text)
        self.assertIn('LineDenoise=120', rp_text)
        self.assertIn('LineDenoiseDirection=2', rp_text)
        self.assertIn('GreenEqThreshold=15', rp_text)

        # 5. LineDenoiseDirection mappings: 'vertical' -> 2, 'both' -> 3, 'horizontal' -> 1
        for d_str, d_val in [('horizontal', '1'), ('vertical', '2'), ('both', '3')]:
            req_d = dict(self.request)
            req_d['adjustments'] = {'raw_preprocess': {'line_noise_direction': d_str}}
            res_d = self.render_dispatch(req_d)
            d_text = (Path(res_d['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
            self.assertIn(f'LineDenoiseDirection={d_val}', d_text)

    def test_phase1_new_adjustments_validation_illegal_types_and_bounds(self):
        """Verify strict rejection of illegal types, bounds, NaNs, and unknown fields."""
        def check_bad_adj(adj):
            req = dict(self.request)
            req['adjustments'] = adj
            self.error('INVALID_REQUEST', self.render_dispatch, req)

        # Empty objects
        check_bad_adj({'highlight_recovery': {}})
        check_bad_adj({'highlight_compression': {}})
        check_bad_adj({'demosaic': {}})
        check_bad_adj({'raw_preprocess': {}})

        # Unknown fields
        check_bad_adj({'highlight_recovery': {'invalid': 1}})
        check_bad_adj({'highlight_compression': {'invalid': 1}})
        check_bad_adj({'demosaic': {'invalid': 1}})
        check_bad_adj({'raw_preprocess': {'invalid': 1}})

        # Bool as number
        check_bad_adj({'highlight_compression': {'amount': True}})
        check_bad_adj({'highlight_compression': {'threshold': False}})
        check_bad_adj({'raw_preprocess': {'hot_dead_pixel_threshold': True}})
        check_bad_adj({'raw_preprocess': {'line_noise': True}})
        check_bad_adj({'raw_preprocess': {'line_noise_direction': True}})
        check_bad_adj({'raw_preprocess': {'green_equilibration': False}})

        # Number as bool
        check_bad_adj({'highlight_recovery': {'enabled': 1}})
        check_bad_adj({'demosaic': {'dual_contrast_auto': 0}})
        check_bad_adj({'raw_preprocess': {'hot_pixel_filter': 1}})
        check_bad_adj({'raw_preprocess': {'dead_pixel_filter': 0}})

        # NaN / Inf
        check_bad_adj({'demosaic': {'method': 'amazevng4', 'dual_contrast': float('nan')}})
        check_bad_adj({'demosaic': {'method': 'amazevng4', 'dual_contrast': float('inf')}})

        # Range bounds
        check_bad_adj({'highlight_compression': {'amount': -1}})
        check_bad_adj({'highlight_compression': {'amount': 501}})
        check_bad_adj({'highlight_compression': {'threshold': -1}})
        check_bad_adj({'highlight_compression': {'threshold': 101}})
        check_bad_adj({'demosaic': {'method': 'amazevng4', 'dual_contrast': -0.1}})
        check_bad_adj({'demosaic': {'method': 'amazevng4', 'dual_contrast': 100.1}})
        check_bad_adj({'raw_preprocess': {'hot_dead_pixel_threshold': 19}})
        check_bad_adj({'raw_preprocess': {'hot_dead_pixel_threshold': 201}})
        check_bad_adj({'raw_preprocess': {'line_noise': -1}})
        check_bad_adj({'raw_preprocess': {'line_noise': 1001}})
        check_bad_adj({'raw_preprocess': {'green_equilibration': -1}})
        check_bad_adj({'raw_preprocess': {'green_equilibration': 101}})

        # Illegal enums
        check_bad_adj({'highlight_recovery': {'method': 'magic'}})
        check_bad_adj({'demosaic': {'method': 'unknown_bayer'}})
        check_bad_adj({'raw_preprocess': {'line_noise_direction': 'diagonal'}})
        check_bad_adj({'raw_preprocess': {'line_noise_direction': 4}})

        # Non-dual demosaic method with dual controls rejected
        check_bad_adj({'demosaic': {'method': 'amaze', 'dual_contrast': 20}})
        check_bad_adj({'demosaic': {'method': 'rcd', 'dual_contrast_auto': False}})

        # First use missing required fields
        check_bad_adj({'highlight_recovery': {'enabled': True}})
        check_bad_adj({'demosaic': {'dual_contrast_auto': True}})

    def test_phase1_new_adjustments_inheritance_and_reset_fields(self):
        """Verify incremental partial inheritance and fine-grained reset_fields."""
        sess_file = self.root / 'phase1_sess.json'
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'Phase 1 测试'
        })
        self.assertEqual(init_res['revision'], 0)

        # Revision 1: Set initial adjustments across categories
        fb1 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'contrast': 15,
                'highlight_recovery': {'enabled': True, 'method': 'blend'},
                'highlight_compression': {'amount': 200, 'threshold': 30},
                'demosaic': {'method': 'amazevng4', 'dual_contrast_auto': False, 'dual_contrast': 25},
                'raw_preprocess': {'hot_pixel_filter': True, 'line_noise': 80}
            }
        })
        self.assertEqual(fb1['revision'], 1)
        eff1 = fb1['effective_adjustments']
        self.assertEqual(eff1['highlight_recovery'], {'enabled': True, 'method': 'blend'})
        self.assertEqual(eff1['demosaic'], {'method': 'amazevng4', 'dual_contrast_auto': False, 'dual_contrast': 25})
        self.assertEqual(eff1['raw_preprocess'], {'hot_pixel_filter': True, 'line_noise': 80})

        # Revision 2: Partial subfield inheritance
        fb2 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {
                'highlight_recovery': {'method': 'coloropp'},
                'demosaic': {'dual_contrast': 45},
                'raw_preprocess': {'dead_pixel_filter': True, 'line_noise': 100}
            }
        })
        self.assertEqual(fb2['revision'], 2)
        eff2 = fb2['effective_adjustments']
        self.assertEqual(eff2['highlight_recovery'], {'enabled': True, 'method': 'coloropp'})
        self.assertEqual(eff2['demosaic'], {'method': 'amazevng4', 'dual_contrast_auto': False, 'dual_contrast': 45})
        self.assertEqual(eff2['raw_preprocess'], {
            'hot_pixel_filter': True, 'dead_pixel_filter': True, 'line_noise': 100
        })
        self.assertEqual(eff2['contrast'], 15)

        # Revision 3: Single-field reset with reset_fields
        fb3 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 2,
            'reset_fields': ['highlight_recovery']
        })
        self.assertEqual(fb3['revision'], 3)
        eff3 = fb3['effective_adjustments']
        self.assertNotIn('highlight_recovery', eff3)
        self.assertIn('demosaic', eff3)
        self.assertIn('raw_preprocess', eff3)
        self.assertIn('highlight_compression', eff3)
        self.assertEqual(eff3['contrast'], 15)

        # Reset multiple fields
        fb4 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 3,
            'reset_fields': ['demosaic', 'raw_preprocess']
        })
        self.assertEqual(fb4['revision'], 4)
        eff4 = fb4['effective_adjustments']
        self.assertNotIn('demosaic', eff4)
        self.assertNotIn('raw_preprocess', eff4)
        self.assertIn('highlight_compression', eff4)
        self.assertEqual(eff4['contrast'], 15)

        # Invalid reset_fields category
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 4,
            'reset_fields': ['unknown_category']
        })
        # Dot notation rejected
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 4,
            'reset_fields': ['demosaic.method']
        })

    def test_phase1_jpeg_and_sensor_restrictions(self):
        """Verify RAW-only adjustments rejected on JPEG, and Bayer rejected on RAF."""
        # 1. Create a synthetic JPEG source
        jpg_source = self.root / 'sample.jpg'
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB', (64, 32), (120, 100, 80)).save(jpg_source, icc_profile=icc)

        # 2. Render on JPEG with RAW-only categories must be rejected
        for raw_cat in ['highlight_recovery', 'demosaic', 'raw_preprocess']:
            req = {
                'action': 'render', 'source': str(jpg_source), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_jpg_fail'), 'format': 'jpeg'
            }
            if raw_cat == 'highlight_recovery':
                req['adjustments'] = {'highlight_recovery': {'enabled': True, 'method': 'blend'}}
            elif raw_cat == 'demosaic':
                req['adjustments'] = {'demosaic': {'method': 'amaze'}}
            else:
                req['adjustments'] = {'raw_preprocess': {'hot_pixel_filter': True}}

            self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, req)

        # 3. highlight_compression IS ALLOWED on JPEG
        req_hc = {
            'action': 'render', 'source': str(jpg_source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_jpg_ok'), 'format': 'jpeg',
            'adjustments': {'highlight_compression': {'amount': 180, 'threshold': 20}}
        }
        res_hc = self.render_dispatch(req_hc)
        hc_text = (Path(res_hc['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
        self.assertIn('[Exposure]', hc_text)
        self.assertIn('HighlightCompr=180', hc_text)
        self.assertIn('HighlightComprThreshold=20', hc_text)

        # 4. Session on JPEG rejects RAW-only adjustments in record_feedback
        sess_jpg = self.root / 'jpg_session.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_jpg), 'source': str(jpg_source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'JPEG session'
        })
        self.error('UNSUPPORTED_ADJUSTMENTS', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_jpg), 'expected_revision': 0,
            'adjustments': {'demosaic': {'method': 'amaze'}}
        })

        # 5. Fujifilm RAF (X-Trans) rejects Bayer demosaic and Bayer preprocess
        raf_source = self.root / 'test.raf'
        raf_source.write_bytes(b'synthetic raf placeholder')
        req_raf_dm = {
            'action': 'render', 'source': str(raf_source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_raf'), 'format': 'jpeg',
            'adjustments': {'demosaic': {'method': 'amaze'}}
        }
        self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, req_raf_dm)

        req_raf_bayer_pre = {
            'action': 'render', 'source': str(raf_source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_raf'), 'format': 'jpeg',
            'adjustments': {'raw_preprocess': {'line_noise': 50}}
        }
        self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, req_raf_bayer_pre)

    def test_phase1_full_session_to_render_manifest_consistency(self):
        """Verify full session -> record_feedback -> render flow and manifest consistency.
        NOTE: Uses mock engine / synthetic placeholder NEF fixture, not real NEF quality or decoding verification.
        """
        sess_file = self.root / 'flow_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'Flow test'
        })

        adj_payload = {
            'highlight_recovery': {'enabled': True, 'method': 'coloropp'},
            'highlight_compression': {'amount': 300, 'threshold': 15},
            'demosaic': {'method': 'rcd'},
            'raw_preprocess': {
                'hot_pixel_filter': True,
                'dead_pixel_filter': True,
                'hot_dead_pixel_threshold': 60,
                'line_noise': 75,
                'line_noise_direction': 'both',
                'green_equilibration': 10
            }
        }
        fb_res = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': adj_payload
        })
        self.assertEqual(fb_res['revision'], 1)

        # Render bound to session
        render_req = {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_flow'), 'format': 'jpeg',
            'session_path': str(sess_file), 'expected_revision': 1
        }
        res = self.render_dispatch(render_req)
        self.assertEqual(res['session_revision'], 2)

        # Inspect manifest
        man_data = json.loads(Path(res['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(man_data['tool_version'], rt.VERSION)
        eff_adj = man_data['effective_adjustments']
        self.assertEqual(eff_adj['highlight_recovery'], {'enabled': True, 'method': 'coloropp'})
        self.assertEqual(eff_adj['highlight_compression'], {'amount': 300, 'threshold': 15})
        self.assertEqual(eff_adj['demosaic'], {'method': 'rcd'})
        self.assertEqual(eff_adj['raw_preprocess'], {
            'hot_pixel_filter': True,
            'dead_pixel_filter': True,
            'hot_dead_pixel_threshold': 60,
            'line_noise': 75,
            'line_noise_direction': 'both',
            'green_equilibration': 10
        })

        # Inspect effective PP3
        eff_pp3 = (Path(res['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
        self.assertIn('[HLRecovery]', eff_pp3)
        self.assertIn('Enabled=true', eff_pp3)
        self.assertIn('Method=Coloropp', eff_pp3)

        self.assertIn('[Exposure]', eff_pp3)
        self.assertIn('HighlightCompr=300', eff_pp3)
        self.assertIn('HighlightComprThreshold=15', eff_pp3)

        self.assertIn('[RAW Bayer]', eff_pp3)
        self.assertIn('Method=rcd', eff_pp3)
        self.assertIn('LineDenoise=75', eff_pp3)
        self.assertIn('LineDenoiseDirection=3', eff_pp3)
        self.assertIn('GreenEqThreshold=10', eff_pp3)

        self.assertIn('[RAW]', eff_pp3)
        self.assertIn('HotPixelFilter=true', eff_pp3)
        self.assertIn('DeadPixelFilter=true', eff_pp3)
        self.assertIn('HotDeadPixelThresh=60', eff_pp3)

    def test_phase2a_raw_calibration_pp3_mapping(self):
        """Verify exact RT 5.13 PP3 section and key names for raw_calibration.
        NOTE: Uses mock engine / synthetic placeholder NEF fixture, not real NEF quality or decoding verification.
        """
        # 1. Independent green1 and green2 -> PreTwoGreen=false
        req = dict(self.request)
        req['adjustments'] = {
            'raw_calibration': {
                'black_offsets': {
                    'red': 10,
                    'green1': 5,
                    'blue': -15,
                    'green2': 25
                },
                'white_scale': 1.25
            }
        }
        res = self.render_dispatch(req)
        eff_pp3 = (Path(res['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
        self.assertIn('[RAW Bayer]', eff_pp3)
        self.assertIn('PreBlack0=5', eff_pp3)    # green1
        self.assertIn('PreBlack1=10', eff_pp3)   # red
        self.assertIn('PreBlack2=-15', eff_pp3)  # blue
        self.assertIn('PreBlack3=25', eff_pp3)   # green2
        self.assertIn('PreTwoGreen=false', eff_pp3)

        self.assertIn('[RAW]', eff_pp3)
        self.assertIn('PreExposure=1.25', eff_pp3)

        # 2. Equal green1 and green2 -> PreTwoGreen=true
        req_eq = dict(self.request)
        req_eq['adjustments'] = {
            'raw_calibration': {
                'black_offsets': {
                    'red': 0,
                    'green1': 8,
                    'blue': 0,
                    'green2': 8
                }
            }
        }
        res_eq = self.render_dispatch(req_eq)
        eff_pp3_eq = (Path(res_eq['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
        self.assertIn('PreBlack0=8', eff_pp3_eq)
        self.assertIn('PreBlack3=8', eff_pp3_eq)
        self.assertIn('PreTwoGreen=true', eff_pp3_eq)

    def test_phase2a_raw_calibration_independent_greens_and_incremental_patch(self):
        """Verify incremental subfield patching of black_offsets and two-green independence."""
        sess_file = self.root / 'phase2a_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'Phase 2A Session'
        })

        # Revision 1: Set initial calibration with separate greens
        fb1 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'raw_calibration': {
                    'black_offsets': {'red': 10, 'green1': 5, 'blue': -5, 'green2': 15},
                    'white_scale': 1.1
                }
            }
        })
        self.assertEqual(fb1['revision'], 1)
        eff1 = fb1['effective_adjustments']['raw_calibration']
        self.assertEqual(eff1['black_offsets'], {'red': 10, 'green1': 5, 'blue': -5, 'green2': 15})
        self.assertEqual(eff1['white_scale'], 1.1)

        # Revision 2: Incrementally patch only 'red'
        fb2 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {
                'raw_calibration': {
                    'black_offsets': {'red': 30}
                }
            }
        })
        self.assertEqual(fb2['revision'], 2)
        eff2 = fb2['effective_adjustments']['raw_calibration']
        # 'red' updated to 30; green1, blue, green2, and white_scale preserved!
        self.assertEqual(eff2['black_offsets'], {'red': 30, 'green1': 5, 'blue': -5, 'green2': 15})
        self.assertEqual(eff2['white_scale'], 1.1)

        # Revision 3: Incrementally patch only 'green1' without overwriting 'green2'
        fb3 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 2,
            'adjustments': {
                'raw_calibration': {
                    'black_offsets': {'green1': 20}
                }
            }
        })
        self.assertEqual(fb3['revision'], 3)
        eff3 = fb3['effective_adjustments']['raw_calibration']
        self.assertEqual(eff3['black_offsets'], {'red': 30, 'green1': 20, 'blue': -5, 'green2': 15})

    def test_phase2a_raw_calibration_reset_fields(self):
        """Verify fine-grained reset_fields removes only raw_calibration."""
        sess_file = self.root / 'phase2a_reset_sess.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'Phase 2A Reset'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'contrast': 25,
                'demosaic': {'method': 'rcd'},
                'raw_calibration': {
                    'black_offsets': {'red': 12},
                    'white_scale': 1.05
                }
            }
        })

        # Reset raw_calibration only
        fb_reset = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'reset_fields': ['raw_calibration']
        })
        self.assertEqual(fb_reset['revision'], 2)
        eff = fb_reset['effective_adjustments']
        self.assertNotIn('raw_calibration', eff)
        self.assertEqual(eff['contrast'], 25)
        self.assertEqual(eff['demosaic'], {'method': 'rcd'})

    def test_phase2a_raw_calibration_validation_bounds_and_types(self):
        """Verify validation of raw_calibration types, bounds, NaNs, and empty objects."""
        def check_bad(adj):
            req = dict(self.request)
            req['adjustments'] = adj
            self.error('INVALID_REQUEST', self.render_dispatch, req)

        # Empty objects
        check_bad({'raw_calibration': {}})
        check_bad({'raw_calibration': {'black_offsets': {}}})

        # Unknown fields
        check_bad({'raw_calibration': {'invalid_field': 1}})
        check_bad({'raw_calibration': {'black_offsets': {'invalid_ch': 0}}})

        # Bool as number
        check_bad({'raw_calibration': {'black_offsets': {'red': True}}})
        check_bad({'raw_calibration': {'black_offsets': {'green1': False}}})
        check_bad({'raw_calibration': {'white_scale': True}})

        # NaN / Inf
        check_bad({'raw_calibration': {'black_offsets': {'red': float('nan')}}})
        check_bad({'raw_calibration': {'black_offsets': {'blue': float('inf')}}})
        check_bad({'raw_calibration': {'white_scale': float('nan')}})
        check_bad({'raw_calibration': {'white_scale': float('inf')}})

        # Bounds
        check_bad({'raw_calibration': {'black_offsets': {'red': -2049}}})
        check_bad({'raw_calibration': {'black_offsets': {'red': 2049}}})
        check_bad({'raw_calibration': {'black_offsets': {'green2': 2500}}})
        check_bad({'raw_calibration': {'white_scale': 0.09}})
        check_bad({'raw_calibration': {'white_scale': 16.1}})

    def test_phase2a_bayer_boundary_tightened_to_nef_only(self):
        """Verify Bayer-exclusive controls strictly restricted to Nikon .nef."""
        # 1. Non-NEF RAW formats
        for ext in ['.arw', '.cr2', '.cr3', '.dng', '.raf', '.orf', '.rw2', '.pef']:
            non_nef = self.root / f'sample{ext}'
            non_nef.write_bytes(b'placeholder raw')

            # Bayer-exclusive demosaic rejected
            self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, {
                'action': 'render', 'source': str(non_nef), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_non_nef'), 'format': 'jpeg',
                'adjustments': {'demosaic': {'method': 'amaze'}}
            })

            # Bayer-exclusive raw_calibration rejected
            self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, {
                'action': 'render', 'source': str(non_nef), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_non_nef'), 'format': 'jpeg',
                'adjustments': {'raw_calibration': {'white_scale': 1.2}}
            })

            # Bayer-exclusive line_noise rejected
            self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, {
                'action': 'render', 'source': str(non_nef), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_non_nef'), 'format': 'jpeg',
                'adjustments': {'raw_preprocess': {'line_noise': 50}}
            })

            # Generic RAW adjustments ARE permitted on non-NEF RAW!
            res_generic = self.render_dispatch({
                'action': 'render', 'source': str(non_nef), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / f'out_generic_{ext[1:]}'), 'format': 'jpeg',
                'adjustments': {
                    'exposure_ev': 0.5,
                    'white_balance': {'temperature': 5200, 'green': 1.0},
                    'raw_preprocess': {'hot_pixel_filter': True}
                }
            })
            eff_pp3 = (Path(res_generic['output']).parent / 'effective.pp3').read_text(encoding='utf-8')
            self.assertIn('Compensation=0.5', eff_pp3)
            self.assertIn('HotPixelFilter=true', eff_pp3)

        # 2. JPEG rejects raw_calibration
        jpg_source = self.root / 'sample_calib.jpg'
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB', (64, 32), (120, 100, 80)).save(jpg_source, icc_profile=icc)

        self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, {
            'action': 'render', 'source': str(jpg_source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_jpg_calib'), 'format': 'jpeg',
            'adjustments': {'raw_calibration': {'white_scale': 1.1}}
        })

        sess_jpg = self.root / 'jpg_calib_session.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_jpg), 'source': str(jpg_source), 'base_profile': str(self.pp3),
            'confirmed_brief': 'JPEG session'
        })
        self.error('UNSUPPORTED_ADJUSTMENTS', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_jpg), 'expected_revision': 0,
            'adjustments': {'raw_calibration': {'white_scale': 1.1}}
        })

    def test_phase2b_lens_correction_and_input_profile_pp3_mapping(self):
        """Verify PP3 serialization for lens_correction and input_profile."""
        lcp_file = self.root / 'sample_lens.lcp'
        lcp_file.write_bytes(b'fake lcp profile data for testing')
        lcp_sha = rt.digest(lcp_file)

        dcp_file = self.root / 'sample_camera.dcp'
        dcp_file.write_bytes(b'fake dcp camera profile data')
        dcp_sha = rt.digest(dcp_file)

        res = self.render_dispatch({
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_phase2b_pp3'),
            'format': 'jpeg',
            'adjustments': {
                'lens_correction': {
                    'mode': 'lcp',
                    'path': str(lcp_file),
                    'sha256': lcp_sha,
                    'distortion': True,
                    'vignetting': False,
                    'ca': True
                },
                'input_profile': {
                    'mode': 'custom',
                    'path': str(dcp_file),
                    'sha256': dcp_sha,
                    'tone_curve': False,
                    'look_table': False,
                    'hue_sat_map': False,
                    'baseline_exposure': True,
                    'dcp_illuminant': 2
                }
            }
        })
        man = json.loads(Path(res['manifest']).read_text())
        pp3_text = (Path(res['manifest']).parent / 'effective.pp3').read_text()

        self.assertIn('[LensProfile]', pp3_text)
        self.assertIn('LcMode=lcp', pp3_text)
        self.assertIn('UseDistortion=true', pp3_text)
        self.assertIn('UseVignette=false', pp3_text)
        self.assertIn('UseCA=true', pp3_text)
        # Snapshot path should be in PP3, not raw canonical path!
        self.assertIn('snapshots/lens_', pp3_text)

        self.assertIn('[Color Management]', pp3_text)
        self.assertIn('ToneCurve=false', pp3_text)
        self.assertIn('ApplyLookTable=false', pp3_text)
        self.assertIn('ApplyBaselineExposureOffset=true', pp3_text)
        self.assertNotIn('ApplyBaselineExposure=', pp3_text.replace('ApplyBaselineExposureOffset=', ''))
        self.assertIn('ApplyHueSatMap=false', pp3_text)
        self.assertIn('DCPIlluminant=2', pp3_text)
        self.assertIn('snapshots/input_', pp3_text)

        # Also test lensfun_manual, lensfun_auto, none, camera_standard, auto_matched
        res2 = self.render_dispatch({
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_phase2b_pp3_2'),
            'format': 'jpeg',
            'adjustments': {
                'lens_correction': {
                    'mode': 'lensfun_manual',
                    'camera_make': 'Nikon',
                    'camera_model': 'NIKON Z 6',
                    'lens': 'NIKKOR Z 24-70mm f/4 S'
                },
                'input_profile': {
                    'mode': 'camera_standard'
                }
            }
        })
        pp3_text2 = (Path(res2['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('LcMode=lfmanual', pp3_text2)
        self.assertIn('LFCameraMake=Nikon', pp3_text2)
        self.assertIn('LFCameraModel=NIKON Z 6', pp3_text2)
        self.assertIn('LFLens=NIKKOR Z 24-70mm f/4 S', pp3_text2)
        self.assertIn('InputProfile=(camera)', pp3_text2)

        res3 = self.render_dispatch({
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_phase2b_pp3_3'),
            'format': 'jpeg',
            'adjustments': {
                'lens_correction': {'mode': 'lensfun_auto'},
                'input_profile': {'mode': 'auto_matched'}
            }
        })
        pp3_text3 = (Path(res3['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('LcMode=lfauto', pp3_text3)
        self.assertIn('InputProfile=(cameraICC)', pp3_text3)

    def test_phase2b_subfield_inheritance_and_mode_switching_requires_reset(self):
        """Verify subfield inheritance and requirement of reset_fields on mode change."""
        sess_file = self.root / 'sess_phase2b_inherit.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source),
            'base_profile': str(self.pp3), 'confirmed_brief': 'Inheritance test'
        })

        # Revision 1: Set lensfun_manual
        fb1 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'lens_correction': {
                    'mode': 'lensfun_manual',
                    'camera_make': 'Nikon',
                    'camera_model': 'Z 6',
                    'lens': 'NIKKOR Z 50mm f/1.8 S'
                }
            }
        })
        eff1 = fb1['effective_adjustments']['lens_correction']
        self.assertEqual(eff1['camera_make'], 'Nikon')

        # Revision 2: Incrementally patch distortion=True without re-supplying make/model/lens
        fb2 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 1,
            'adjustments': {
                'lens_correction': {'distortion': True}
            }
        })
        eff2 = fb2['effective_adjustments']['lens_correction']
        self.assertEqual(eff2['mode'], 'lensfun_manual')
        self.assertEqual(eff2['camera_make'], 'Nikon')
        self.assertEqual(eff2['camera_model'], 'Z 6')
        self.assertEqual(eff2['lens'], 'NIKKOR Z 50mm f/1.8 S')
        self.assertTrue(eff2['distortion'])

        # Attempt to switch mode to lensfun_auto WITHOUT reset_fields -> should fail!
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 2,
            'adjustments': {
                'lens_correction': {'mode': 'lensfun_auto'}
            }
        })

        # Switch mode to lensfun_auto WITH reset_fields -> succeeds!
        fb3 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 2,
            'reset_fields': ['lens_correction'],
            'adjustments': {
                'lens_correction': {'mode': 'lensfun_auto'}
            }
        })
        eff3 = fb3['effective_adjustments']['lens_correction']
        self.assertEqual(eff3, {'mode': 'lensfun_auto'})

    def test_phase2b_external_resource_snapshot_and_immutability(self):
        """Verify snapshotting, manifest recording, and immutability of original assets."""
        lcp_file = self.root / 'immut_lens.lcp'
        lcp_file.write_bytes(b'immutability test lcp data')
        lcp_sha = rt.digest(lcp_file)

        src_hash_before = rt.digest(self.source)
        pp3_hash_before = rt.digest(self.pp3)
        lcp_hash_before = rt.digest(lcp_file)

        res = self.render_dispatch({
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': pp3_hash_before,
            'engine_version': '5.13',
            'output_dir': str(self.root / 'out_immut'),
            'format': 'jpeg',
            'adjustments': {
                'lens_correction': {'mode': 'lcp', 'path': str(lcp_file), 'sha256': lcp_sha}
            }
        })

        # Assert immutability of inputs
        self.assertEqual(rt.digest(self.source), src_hash_before)
        self.assertEqual(rt.digest(self.pp3), pp3_hash_before)
        self.assertEqual(rt.digest(lcp_file), lcp_hash_before)

        # Assert snapshot copy exists in render directory
        manifest = json.loads(Path(res['manifest']).read_text())
        ext_res = manifest.get('external_resources', {})
        self.assertIn('lens_profile', ext_res)
        snap_path = Path(ext_res['lens_profile']['snapshot_path'])
        self.assertTrue(snap_path.is_file())
        self.assertEqual(rt.digest(snap_path), lcp_sha)

        # Assert verification status and pipeline stages
        self.assertFalse(manifest['pipeline_stages']['effect_verified'])
        self.assertTrue(manifest['pipeline_stages']['serialized'])
        self.assertTrue(manifest['pipeline_stages']['render_completed'])
        self.assertFalse(manifest['verification']['effect_verified'])
        self.assertEqual(res['pipeline_stages']['effect_verified'], False)
        self.assertEqual(res['verification']['lens_correction_effect_verified'], False)

    def test_phase2b_tampered_resource_and_re_verification_on_inherited_revision(self):
        """Verify that inherited resources are re-verified on disk on subsequent renders."""
        lcp_file = self.root / 'tamper_lens.lcp'
        lcp_file.write_bytes(b'original lcp bytes')
        lcp_sha = rt.digest(lcp_file)

        sess_file = self.root / 'sess_tamper.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source),
            'base_profile': str(self.pp3), 'confirmed_brief': 'Tamper test'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'lens_correction': {'mode': 'lcp', 'path': str(lcp_file), 'sha256': lcp_sha}
            }
        })

        # Tamper with the LCP file before render!
        lcp_file.write_bytes(b'tampered modified lcp bytes')

        self.error('CHECKSUM_MISMATCH', self.render_dispatch, {
            'action': 'render', 'source': str(self.source),
            'profile': str(self.pp3), 'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13', 'output_dir': str(self.root / 'out_tamper'),
            'format': 'jpeg', 'session_path': str(sess_file), 'expected_revision': 1
        })

    def test_phase2b_profile_fingerprint_action(self):
        """Verify profile action with kind=dcp|icc|lcp returns fingerprint metadata."""
        lcp = self.root / 'fp.lcp'
        lcp.write_bytes(b'lcp fingerprint test')
        lcp_res = rt.dispatch({'action': 'profile', 'profile': str(lcp), 'kind': 'lcp'})
        self.assertEqual(lcp_res['kind'], 'lcp')
        self.assertEqual(lcp_res['sha256'], rt.digest(lcp))
        self.assertEqual(lcp_res['verification_status'], 'file_integrity_verified')

        dcp = self.root / 'fp.dcp'
        dcp.write_bytes(b'dcp fingerprint test')
        dcp_res = rt.dispatch({'action': 'profile', 'profile': str(dcp), 'kind': 'dcp'})
        self.assertEqual(dcp_res['kind'], 'dcp')
        self.assertEqual(dcp_res['sha256'], rt.digest(dcp))

        # Kind extension mismatch
        self.error('INVALID_REQUEST', rt.dispatch, {'action': 'profile', 'profile': str(lcp), 'kind': 'dcp'})

    def test_phase2b_rejections_and_boundaries(self):
        """Verify boundary rejections: path injection, illegal mode, JPEG source, etc."""
        # 1. Illegal mode 'metadata' (unsupported for Nikon NEF in RT 5.13)
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_meta'), 'format': 'jpeg',
            'adjustments': {'lens_correction': {'mode': 'metadata'}}
        })

        # 2. Path injection with newline or NUL
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_inj'), 'format': 'jpeg',
            'adjustments': {'lens_correction': {'mode': 'lcp', 'path': '/path/to\nlens.lcp', 'sha256': 'a'*64}}
        })

        # 3. JPEG source rejection
        jpg_source = self.root / 'test_rej.jpg'
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB', (64, 32), (120, 100, 80)).save(jpg_source, icc_profile=icc)

        self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, {
            'action': 'render', 'source': str(jpg_source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_jpg_lens'), 'format': 'jpeg',
            'adjustments': {'lens_correction': {'mode': 'lensfun_auto'}}
        })
        self.error('UNSUPPORTED_ADJUSTMENTS', self.render_dispatch, {
            'action': 'render', 'source': str(jpg_source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_jpg_input'), 'format': 'jpeg',
            'adjustments': {'input_profile': {'mode': 'camera_standard'}}
        })

    def test_phase2b_clean_safeguards_active_session_resources(self):
        """Verify clean preserves external resources referenced in active sessions."""
        lcp = self.root / 'clean_test.lcp'
        lcp.write_bytes(b'protected lcp')
        lcp_sha = rt.digest(lcp)

        sess_file = self.root / 'sess_clean.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source),
            'base_profile': str(self.pp3), 'confirmed_brief': 'Clean test'
        })
        rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_file), 'expected_revision': 0,
            'adjustments': {
                'lens_correction': {'mode': 'lcp', 'path': str(lcp), 'sha256': lcp_sha}
            }
        })

        clean_out = self.root / 'clean_out'
        clean_out.mkdir(parents=True, exist_ok=True)
        res = rt.dispatch({
            'action': 'clean',
            'session_path': str(sess_file),
            'output_dir': str(clean_out),
            'apply': True,
            'min_age_seconds': 0
        })
        self.assertEqual(res['total_orphans'], 0)
        self.assertTrue(lcp.is_file())

    def test_phase3a_contract_semantics_and_corrections(self):
        """Verify 6 Phase 3A contract semantics and corrections."""
        # 1. Demosaic: exactly 15 photographic algorithms, removed fast/mono/pixelshift/none rejected
        self.assertEqual(len(rt.BAYER_DEMOSAIC_METHODS), 15)
        caps = rt.dispatch({'action': 'capabilities', 'source_type': 'raw'})
        caps_methods = caps['controllable_parameters']['demosaic']['method']['enum']
        self.assertEqual(caps_methods, sorted(list(rt.BAYER_DEMOSAIC_METHODS)))
        for rm in ['fast', 'mono', 'pixelshift', 'none']:
            self.assertNotIn(rm, rt.BAYER_DEMOSAIC_METHODS)
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_p3a_dm'), 'format': 'jpeg',
                'adjustments': {'demosaic': {'method': rm}}
            })

        # 2. Line noise direction: only horizontal, vertical, both; pdaf_lines / 5 rejected
        caps_dirs = caps['controllable_parameters']['raw_preprocess']['line_noise_direction']['enum']
        self.assertEqual(caps_dirs, ['horizontal', 'vertical', 'both'])
        for bad_dir in ['pdaf_lines', 'pdaf', 5, '5']:
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
                'output_dir': str(self.root / 'out_p3a_dir'), 'format': 'jpeg',
                'adjustments': {'raw_preprocess': {'line_noise_direction': bad_dir}}
            })

        # 3. A manual threshold implies auto=False; explicit contradiction is rejected.
        res_manual = self.render_dispatch({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_p3a_dc1'), 'format': 'jpeg',
            'adjustments': {'demosaic': {'method': 'amazevng4', 'dual_contrast': 25.0}}
        })
        manual_pp3 = (Path(res_manual['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('DualDemosaicAutoContrast=false', manual_pp3)
        self.assertIn('DualDemosaicContrast=25', manual_pp3)
        # Explicit dual_contrast_auto=True with dual_contrast:
        self.error('INVALID_REQUEST', rt.dispatch, {
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_p3a_dc2'), 'format': 'jpeg',
            'adjustments': {'demosaic': {'method': 'amazevng4', 'dual_contrast_auto': True, 'dual_contrast': 25.0}}
        })
        # Valid initial use:
        sess_dc = self.root / 'sess_dc.json'
        rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_dc), 'source': str(self.source),
            'base_profile': str(self.pp3), 'confirmed_brief': 'Dual contrast test'
        })
        fb1 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_dc), 'expected_revision': 0,
            'adjustments': {'demosaic': {'method': 'amazevng4', 'dual_contrast_auto': False, 'dual_contrast': 20.0}}
        })
        self.assertEqual(fb1['effective_adjustments']['demosaic']['dual_contrast_auto'], False)
        # Incremental patch of only dual_contrast: inherits dual_contrast_auto=False!
        fb2 = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_dc), 'expected_revision': 1,
            'adjustments': {'demosaic': {'dual_contrast': 30.0}}
        })
        self.assertEqual(fb2['effective_adjustments']['demosaic']['dual_contrast_auto'], False)
        self.assertEqual(fb2['effective_adjustments']['demosaic']['dual_contrast'], 30.0)

        # 4. Dehablack=false on manual black_offsets; NOT on white_scale alone
        res_bo = self.render_dispatch({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_p3a_bo'), 'format': 'jpeg',
            'adjustments': {'raw_calibration': {'black_offsets': {'red': 15}}}
        })
        pp3_bo = (Path(res_bo['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('Dehablack=false', pp3_bo)
        self.assertIn('PreBlack1=15', pp3_bo)

        res_ws = self.render_dispatch({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_p3a_ws'), 'format': 'jpeg',
            'adjustments': {'raw_calibration': {'white_scale': 1.15}}
        })
        pp3_ws = (Path(res_ws['manifest']).parent / 'effective.pp3').read_text()
        self.assertNotIn('Dehablack=', pp3_ws)
        self.assertIn('PreExposure=1.15', pp3_ws)

        # 5. dcp_illuminant description updated, not fixed to StdA/D65
        dcp_desc = caps['controllable_parameters']['input_profile']['properties']['dcp_illuminant']['desc']
        self.assertIn('first calibration illuminant', dcp_desc)
        self.assertIn('second calibration illuminant', dcp_desc)
        self.assertNotIn('1=stdA', dcp_desc)
        self.assertNotIn('2=D65', dcp_desc)

        # 6. Baseline exposure: only ApplyBaselineExposureOffset, strictly no ApplyBaselineExposure alias
        dcp_test = self.root / 'p3a.dcp'
        dcp_test.write_bytes(b'dcp test')
        res_dcp = self.render_dispatch({
            'action': 'render', 'source': str(self.source), 'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3), 'engine_version': '5.13',
            'output_dir': str(self.root / 'out_p3a_dcp'), 'format': 'jpeg',
            'adjustments': {
                'input_profile': {
                    'mode': 'custom', 'path': str(dcp_test), 'sha256': rt.digest(dcp_test),
                    'baseline_exposure': True
                }
            }
        })
        pp3_dcp = (Path(res_dcp['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('ApplyBaselineExposureOffset=true', pp3_dcp)
        # Verify no standalone ApplyBaselineExposure without Offset
        self.assertNotIn('ApplyBaselineExposure=', pp3_dcp.replace('ApplyBaselineExposureOffset=', ''))

    def test_phase3b_raw_qc_and_nef_sniffing(self):
        """Phase 3B: RAW input basic check, NEF content sniffing, and raw_qc CFA diagnostics."""
        # 1. NEF Container Header Sniffing
        # Reject JPEG masquerading as .NEF
        fake_jpg = self.root / 'fake_nef.NEF'
        fake_jpg.write_bytes(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {**self.request, 'source': str(fake_jpg)})
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'inspect', 'source': str(fake_jpg)})
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(fake_jpg)})
        self.error('UNSUPPORTED_INPUT', rt.session_init, {
            'session_path': str(self.root / 's_fake.json'), 'source': str(fake_jpg),
            'base_profile': str(self.pp3), 'confirmed_brief': 'brief'
        })

        # Reject PNG masquerading as .NEF
        fake_png = self.root / 'fake_png.NEF'
        fake_png.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(fake_png)})

        # Reject plain text masquerading as .NEF
        fake_txt = self.root / 'fake_txt.NEF'
        fake_txt.write_bytes(b'Plain text not a TIFF header')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(fake_txt)})

        # Reject short file (<4 bytes)
        fake_short = self.root / 'fake_short.NEF'
        fake_short.write_bytes(b'II')
        self.error('CORRUPT_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(fake_short)})

        # Reject non-NEF in raw_qc
        sample_jpg = self.root / 'sample.jpg'
        Image.new('RGB', (16, 16)).save(sample_jpg, 'JPEG')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(sample_jpg)})
        sample_arw = self.root / 'sample.arw'
        sample_arw.write_bytes(b'II\x2a\x00sony arw raw')
        self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(sample_arw)})

        # Big-endian TIFF magic header is also accepted
        be_nef = self.root / 'be.NEF'
        be_nef.write_bytes(b'MM\x00\x2aBigEndianTIFFHeaderMock')
        be_info = rt.check_source(be_nef)
        self.assertEqual(be_info['source_type'], 'raw')
        self.assertEqual(be_info['byte_order'], 'big')
        self.assertEqual(be_info['container_check'], 'tiff_header_valid')

        # 2. Capabilities & Doctor checks
        caps = rt.dispatch({'action': 'capabilities'})
        self.assertIn('raw_qc', caps['supported_actions'])
        self.assertIn('raw_qc', caps['source_restrictions']['raw'])

        doc = rt.dispatch({'action': 'doctor'})
        self.assertIn('rawpy', doc)
        self.assertIn('numpy', doc)
        self.assertIn('raw_qc_available', doc)
        self.assertIn('raw_qc_install_hint', doc)
        self.assertTrue(doc['numpy'])
        # Without rawpy installed:
        self.assertFalse(doc['rawpy'])
        self.assertFalse(doc['raw_qc_available'])
        self.assertIn('rawpy', doc['raw_qc_install_hint'])

        # Direct call to raw_qc without mock raises DEPENDENCY_MISSING
        self.error('DEPENDENCY_MISSING', rt.dispatch, {'action': 'raw_qc', 'source': str(self.source)})

        # 3. CFA Diagnostic Mathematical Verification with Synthetic Mock
        class FakeRawContext:
            def __init__(self, img, colors, black_per_ch, white_per_ch, white_level=None, color_desc=b'RGBG', pattern=None):
                self.raw_image_visible = img
                self.raw_colors_visible = colors
                self.black_level_per_channel = black_per_ch
                self.camera_white_level_per_channel = white_per_ch
                self.white_level = white_level
                self.color_desc = color_desc
                self.raw_pattern = pattern if pattern is not None else np.array([[0, 1], [3, 2]])
                self.libraw_version = '0.21.3'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        # 4x4 Synthetic CFA sensor with 4 channels (R=0, G1=1, B=2, G2=3)
        colors_4x4 = np.array([
            [0, 1, 0, 1],
            [3, 2, 3, 2],
            [0, 1, 0, 1],
            [3, 2, 3, 2]
        ], dtype=np.int32)

        # Values per channel:
        # R (0):  [500, 15000, 1000, 16000] -> min=500, max=16000, mean=8125.0
        # G1 (1): [1000, 13000, 2000, 15000] -> min=1000, max=15000, mean=7750.0
        # B (2):  [300, 400, 1000, 2000] -> min=300, max=2000, mean=925.0
        # G2 (3): [512, 600, 14000, 14500] -> min=512, max=14500, mean=7403.0
        img_4x4 = np.array([
            [500,   1000, 15000, 13000],
            [512,   300,  600,   400],
            [1000,  2000, 16000, 15000],
            [14000, 1000, 14500, 2000]
        ], dtype=np.uint16)

        fake_rawpy = types.ModuleType('rawpy')
        fake_rawpy.__version__ = '0.21.3'
        fake_rawpy.libraw_version = '0.21.3'

        mock_obj = FakeRawContext(
            img=img_4x4,
            colors=colors_4x4,
            black_per_ch=[512, 512, 512, 512],
            white_per_ch=[14000, 14500, None, 14000],  # B missing per-channel white
            white_level=15000                          # Global white level fallback
        )
        fake_rawpy.imread = MagicMock(return_value=mock_obj)

        with patch.dict(sys.modules, {'rawpy': fake_rawpy}):
            res = rt.dispatch({'action': 'raw_qc', 'source': str(self.source)})

            self.assertEqual(res['source'], str(self.source))
            self.assertEqual(res['source_type'], 'raw')
            self.assertEqual(res['container_check'], 'tiff_header_valid')
            self.assertEqual(res['decoder']['name'], 'rawpy')
            self.assertEqual(res['raw_dimensions']['visible_width'], 4)
            self.assertEqual(res['raw_dimensions']['visible_height'], 4)
            self.assertEqual(res['raw_dimensions']['color_desc'], 'RGBG')
            self.assertFalse(res['roi']['applied'])
            self.assertEqual(len(res['channels']), 4)

            # R (Channel 0)
            ch0 = res['channels'][0]
            self.assertEqual(ch0['channel_index'], 0)
            self.assertEqual(ch0['color_desc'], 'R')
            self.assertEqual(ch0['sample_count'], 4)
            self.assertEqual(ch0['min'], 500)
            self.assertEqual(ch0['max'], 16000)
            self.assertEqual(ch0['mean'], 8125.0)
            self.assertEqual(ch0['black_level'], 512.0)
            self.assertEqual(ch0['white_level'], 14000.0)
            self.assertEqual(ch0['threshold_sources']['black'], 'black_level_per_channel')
            self.assertEqual(ch0['threshold_sources']['white'], 'camera_white_level_per_channel')
            self.assertEqual(ch0['sub_black_pct'], 25.0)   # 500 <= 512 (1/4)
            self.assertEqual(ch0['clip_white_pct'], 50.0)  # 15000, 16000 >= 14000 (2/4)

            # G1 (Channel 1)
            ch1 = res['channels'][1]
            self.assertEqual(ch1['channel_index'], 1)
            self.assertEqual(ch1['color_desc'], 'G')
            self.assertEqual(ch1['white_level'], 14500.0)
            self.assertEqual(ch1['sub_black_pct'], 0.0)     # none <= 512
            self.assertEqual(ch1['clip_white_pct'], 25.0)  # 15000 >= 14500 (1/4)

            # B (Channel 2) - tests missing per-channel fallback to global white_level
            ch2 = res['channels'][2]
            self.assertEqual(ch2['channel_index'], 2)
            self.assertEqual(ch2['color_desc'], 'B')
            self.assertEqual(ch2['white_level'], 15000.0)
            self.assertEqual(ch2['threshold_sources']['white'], 'decoder_white_level_fallback')
            self.assertEqual(ch2['sub_black_pct'], 50.0)   # 300, 400 <= 512 (2/4)
            self.assertEqual(ch2['clip_white_pct'], 0.0)   # none >= 15000

            # G2 (Channel 3) - independent from G1
            ch3 = res['channels'][3]
            self.assertEqual(ch3['channel_index'], 3)
            self.assertEqual(ch3['color_desc'], 'G')
            self.assertEqual(ch3['white_level'], 14000.0)  # G2 has 14000 while G1 has 14500
            self.assertEqual(ch3['sub_black_pct'], 25.0)   # 512 <= 512 (1/4)
            self.assertEqual(ch3['clip_white_pct'], 50.0)  # 14000, 14500 >= 14000 (2/4)

            # Disclaimers present
            self.assertIn('scope_and_disclaimers', res)
            self.assertTrue(res['scope_and_disclaimers']['visual_review_required'])

            # 4. Valid ROI cropping
            res_roi = rt.dispatch({
                'action': 'raw_qc', 'source': str(self.source),
                'roi': {'x': 0, 'y': 0, 'width': 2, 'height': 2}
            })
            self.assertTrue(res_roi['roi']['applied'])
            self.assertEqual(res_roi['roi']['x'], 0)
            self.assertEqual(res_roi['roi']['y'], 0)
            self.assertEqual(res_roi['roi']['width'], 2)
            self.assertEqual(res_roi['roi']['height'], 2)
            # Top-left 2x2 has 1 pixel per channel: (0,0)=R(500), (0,1)=G1(1000), (1,0)=G2(512), (1,1)=B(300)
            self.assertEqual(res_roi['channels'][0]['sample_count'], 1)
            self.assertEqual(res_roi['channels'][0]['min'], 500)
            self.assertEqual(res_roi['channels'][0]['max'], 500)
            self.assertEqual(res_roi['channels'][0]['mean'], 500.0)

            # 5. Invalid ROI boundary / parameter checks
            # Out of bounds x + width > 4
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'raw_qc', 'source': str(self.source),
                'roi': {'x': 3, 'y': 0, 'width': 2, 'height': 2}
            })
            # Out of bounds y + height > 4
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'raw_qc', 'source': str(self.source),
                'roi': {'x': 0, 'y': 3, 'width': 2, 'height': 2}
            })
            # Negative coordinates
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'raw_qc', 'source': str(self.source),
                'roi': {'x': -1, 'y': 0, 'width': 2, 'height': 2}
            })
            # Zero width
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'raw_qc', 'source': str(self.source),
                'roi': {'x': 0, 'y': 0, 'width': 0, 'height': 2}
            })
            # Unknown fields in ROI
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'raw_qc', 'source': str(self.source),
                'roi': {'x': 0, 'y': 0, 'width': 2, 'height': 2, 'unexpected': 1}
            })

            # 6. No Credible White Level scenario (no per_channel white, no global white)
            mock_nowhite = FakeRawContext(
                img=img_4x4, colors=colors_4x4,
                black_per_ch=[512, 512, 512, 512],
                white_per_ch=None,
                white_level=None
            )
            fake_rawpy.imread = MagicMock(return_value=mock_nowhite)
            res_nowhite = rt.dispatch({'action': 'raw_qc', 'source': str(self.source)})
            ch0_nw = res_nowhite['channels'][0]
            self.assertIsNone(ch0_nw['white_level'])
            self.assertEqual(ch0_nw['threshold_sources']['white'], 'no_credible_white_level')
            self.assertIsNone(ch0_nw['clip_white_pct'])
            # Ensure observed max (16000) was NOT used to fake white level
            self.assertNotEqual(ch0_nw['white_level'], 16000)

            # 7. Invalid Levels scenario (white <= black)
            mock_inv = FakeRawContext(
                img=img_4x4, colors=colors_4x4,
                black_per_ch=[2000, 2000, 2000, 2000],
                white_per_ch=None,
                white_level=1000  # white <= black!
            )
            fake_rawpy.imread = MagicMock(return_value=mock_inv)
            res_inv = rt.dispatch({'action': 'raw_qc', 'source': str(self.source)})
            ch0_inv = res_inv['channels'][0]
            self.assertIsNone(ch0_inv['white_level'])
            self.assertEqual(ch0_inv['threshold_sources']['white'], 'invalid_levels_white_le_black')
            self.assertIsNone(ch0_inv['clip_white_pct'])
            # Ensure allow_nan=False dumps cleanly without NaN/Inf
            json_dump = json.dumps(res_inv, allow_nan=False)
            self.assertIn('"white_level": null', json_dump)

            # 8. Non-Bayer CFA layout rejection
            mock_nonbayer = FakeRawContext(
                img=np.zeros((4, 4), dtype=np.uint16),
                colors=np.array([[0, 1, 2, 4], [0, 1, 2, 4], [0, 1, 2, 4], [0, 1, 2, 4]], dtype=np.int32),  # 4 channels but index 4 with len 4
                black_per_ch=[512, 512, 512, 512],
                white_per_ch=[14000, 14000, 14000, 14000]
            )
            fake_rawpy.imread = MagicMock(return_value=mock_nonbayer)
            self.error('UNSUPPORTED_INPUT', rt.dispatch, {'action': 'raw_qc', 'source': str(self.source)})

        # 9. CLI standard error envelope check (no traceback)
        req_file = self.root / 'bad_req.json'
        req_file.write_text(json.dumps({'action': 'raw_qc', 'source': str(self.source)}))
        cmd = [sys.executable, str(Path(rt.__file__).resolve()), '--request', str(req_file)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        cli_out = json.loads(proc.stdout)
        self.assertFalse(cli_out['ok'])
        self.assertEqual(cli_out['error']['code'], 'DEPENDENCY_MISSING')
        self.assertIn('rawpy', cli_out['error']['message'])
        self.assertEqual(proc.stderr.strip(), '')

    def test_phase4a_clean_resource_protection_direction_fix(self):
        """Phase 4A: verify clean resource protection direction fix."""
        out_dir = self.root / 'clean_p4a_output'
        out_dir.mkdir(parents=True, exist_ok=True)

        sess_file = self.root / 'clean_p4a_sess.json'
        init_res = rt.dispatch({
            'action': 'session', 'operation': 'init',
            'session_path': str(sess_file), 'source': str(self.source), 'base_profile': str(self.pp3),
            'session_id': 'sess_p4a_clean_test',
            'confirmed_brief': 'Phase 4A clean resource protection test'
        })
        sess_id = init_res['session_id']

        # 1. Orphan 1: Legal orphan directory containing an input_profile snapshot file
        orphan1_with_snap = out_dir / 'orphan_snap__rt-5.13__abc-111'
        orphan1_with_snap.mkdir(parents=True, exist_ok=True)
        (orphan1_with_snap / '.active.lock').touch()
        (orphan1_with_snap / 'output.jpg').write_bytes(b'render output bytes')
        (orphan1_with_snap / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': sess_id,
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')
        snap_dir = orphan1_with_snap / 'snapshots'
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_dcp = snap_dir / 'input_test_profile.dcp'
        snap_bytes = b'DCP custom profile content inside orphan'
        snap_dcp.write_bytes(snap_bytes)

        # 2. Orphan 2: Legal orphan directory without any protected file in the same out_dir
        orphan2_unreferenced = out_dir / 'orphan_unref__rt-5.13__abc-222'
        orphan2_unreferenced.mkdir(parents=True, exist_ok=True)
        (orphan2_unreferenced / '.active.lock').touch()
        (orphan2_unreferenced / 'output.jpg').write_bytes(b'orphan 2 output')
        (orphan2_unreferenced / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': sess_id,
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # 3. Orphan 3: Prefix collision - name starts with orphan_snap prefix, but is NOT a child
        orphan3_prefix_col = out_dir / 'orphan_snap_collision__rt-5.13__abc-333'
        orphan3_prefix_col.mkdir(parents=True, exist_ok=True)
        (orphan3_prefix_col / '.active.lock').touch()
        (orphan3_prefix_col / 'output.jpg').write_bytes(b'orphan 3 output')
        (orphan3_prefix_col / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': sess_id,
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # 4. Orphan 4: Committed to history in the same out_dir
        orphan4_committed = out_dir / 'orphan_committed__rt-5.13__abc-444'
        orphan4_committed.mkdir(parents=True, exist_ok=True)
        (orphan4_committed / '.active.lock').touch()
        committed_jpg = orphan4_committed / 'committed.jpg'
        committed_jpg.write_bytes(b'committed output bytes')
        committed_man = orphan4_committed / 'manifest.json'
        committed_man.write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': sess_id,
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # Update session:
        # - effective_adjustments references snap_dcp inside orphan1_with_snap
        # - history[0] commits orphan4_committed
        s_data = json.loads(sess_file.read_text(encoding='utf-8'))
        s_data['effective_adjustments'] = {
            'input_profile': {
                'mode': 'custom',
                'path': str(snap_dcp),
                'sha256': rt.digest(snap_dcp)
            }
        }
        s_data['history'].append({
            'revision': 0,
            'committed_revision': 1,
            'timestamp': '2026-10-23T12:00:00Z',
            'adjustments': {},
            'manifest_path': str(committed_man),
            'manifest_sha256': rt.digest(committed_man),
            'output_path': str(committed_jpg),
            'output_sha256': rt.digest(committed_jpg),
            'status': 'SUCCESS'
        })
        sess_file.write_text(json.dumps(s_data), encoding='utf-8')

        # TEST A: Dry-run (apply=False, min_age_seconds=0)
        res_dry = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': False, 'min_age_seconds': 0
        })
        dry_skipped = {Path(s['path']).name: s['reason'] for s in res_dry['skipped']}
        self.assertEqual(dry_skipped.get(orphan1_with_snap.name), 'protected_user_file_inside')
        self.assertEqual(dry_skipped.get(orphan4_committed.name), 'committed_to_session_history')
        # orphan2 and orphan3 are candidate orphans (NOT skipped!)
        dry_cand_names = [Path(o['path']).name for o in res_dry['orphans']]
        self.assertIn(orphan2_unreferenced.name, dry_cand_names)
        self.assertIn(orphan3_prefix_col.name, dry_cand_names)
        self.assertNotIn(orphan1_with_snap.name, dry_cand_names)
        self.assertNotIn(orphan4_committed.name, dry_cand_names)
        # All directories and files exist and untouched
        self.assertTrue(orphan1_with_snap.exists())
        self.assertEqual(snap_dcp.read_bytes(), snap_bytes)
        self.assertTrue(orphan2_unreferenced.exists())
        self.assertTrue(orphan3_prefix_col.exists())
        self.assertTrue(orphan4_committed.exists())

        # TEST B: Apply (apply=True, min_age_seconds=0)
        res_apply = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 0
        })
        apply_skipped = {Path(s['path']).name: s['reason'] for s in res_apply['skipped']}
        self.assertEqual(apply_skipped.get(orphan1_with_snap.name), 'protected_user_file_inside')
        self.assertEqual(apply_skipped.get(orphan4_committed.name), 'committed_to_session_history')
        # Orphan 1 and Orphan 4 are KEPT
        self.assertTrue(orphan1_with_snap.exists(), 'Orphan 1 containing protected resource must NOT be deleted')
        self.assertEqual(snap_dcp.read_bytes(), snap_bytes, 'Bytes in protected resource must remain unchanged')
        self.assertTrue(orphan4_committed.exists(), 'Committed folder must NOT be deleted')
        # Orphan 2 (unreferenced) and Orphan 3 (prefix collision) are ACTUALLY DELETED
        self.assertFalse(orphan2_unreferenced.exists(), 'Unreferenced orphan in same parent dir must be truly deleted')
        self.assertFalse(orphan3_prefix_col.exists(), 'Prefix collision must not prevent orphan deletion')

        # TEST C: Historical adjustment reference protection
        orphan2_history_ref = out_dir / 'orphan_history_ref__rt-5.13__abc-555'
        orphan2_history_ref.mkdir(parents=True, exist_ok=True)
        (orphan2_history_ref / '.active.lock').touch()
        (orphan2_history_ref / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': sess_id,
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')
        hist_lcp = orphan2_history_ref / 'snapshots' / 'hist_lens.lcp'
        hist_lcp.parent.mkdir(parents=True, exist_ok=True)
        hist_lcp.write_bytes(b'LCP profile in history')

        # Add to history adjustments without marking output as committed
        s_data = json.loads(sess_file.read_text(encoding='utf-8'))
        s_data['history'].append({
            'revision': 1,
            'committed_revision': 2,
            'timestamp': '2026-10-23T13:00:00Z',
            'adjustments': {
                'lens_correction': {
                    'mode': 'lcp',
                    'path': str(hist_lcp),
                    'sha256': rt.digest(hist_lcp)
                }
            },
            'output_path': str(self.root / 'other_output.jpg'),
            'status': 'SUCCESS'
        })
        sess_file.write_text(json.dumps(s_data), encoding='utf-8')

        res_hist_apply = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 0
        })
        hist_skipped = {Path(s['path']).name: s['reason'] for s in res_hist_apply['skipped']}
        self.assertEqual(hist_skipped.get(orphan2_history_ref.name), 'protected_user_file_inside')
        self.assertTrue(orphan2_history_ref.exists(), 'Directory with historical resource reference must be preserved')

        # TEST D: Symlink reference preserves canonical safety semantics
        orphan5_sym_target = out_dir / 'orphan_sym_target__rt-5.13__abc-666'
        orphan5_sym_target.mkdir(parents=True, exist_ok=True)
        (orphan5_sym_target / '.active.lock').touch()
        (orphan5_sym_target / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': sess_id,
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')
        real_target_file = orphan5_sym_target / 'real_profile.dcp'
        real_target_file.write_bytes(b'real dcp profile')

        outside_symlink = self.root / 'outside_symlink.dcp'
        if outside_symlink.is_symlink() or outside_symlink.exists():
            outside_symlink.unlink()
        outside_symlink.symlink_to(real_target_file)

        s_data = json.loads(sess_file.read_text(encoding='utf-8'))
        s_data['effective_adjustments'] = {
            'input_profile': {
                'mode': 'custom',
                'path': str(outside_symlink),
                'sha256': rt.digest(real_target_file)
            }
        }
        sess_file.write_text(json.dumps(s_data), encoding='utf-8')

        res_sym_apply = rt.dispatch({
            'action': 'clean', 'output_dir': str(out_dir), 'session_path': str(sess_file),
            'apply': True, 'min_age_seconds': 0
        })
        sym_skipped = {Path(s['path']).name: s['reason'] for s in res_sym_apply['skipped']}
        self.assertEqual(sym_skipped.get(orphan5_sym_target.name), 'protected_user_file_inside')
        self.assertTrue(orphan5_sym_target.exists(), 'Canonical resolution must protect directory holding symlink target')

    def test_phase4b_contract_schema_and_runtime_oracle(self):
        import jsonschema
        validator = jsonschema.Draft202012Validator(rt.schema())

        def assert_valid_in_both(req):
            errors = list(validator.iter_errors(req))
            self.assertEqual(errors, [], f"Schema validation unexpectedly failed: {[e.message for e in errors]}")
            normalized = rt.validate(req.copy())
            self.assertIsInstance(normalized, dict)

        def assert_invalid_in_both(req, expected_err_code='INVALID_REQUEST'):
            errors = list(validator.iter_errors(req))
            self.assertTrue(len(errors) > 0, f"Draft202012Validator should reject invalid request but accepted it: {req}")
            self.error(expected_err_code, rt.validate, req.copy())

        # -------------------------------------------------------------
        # 1. CLEAN: mutual exclusion of session_path & session_paths, minItems=1, uniqueItems=True
        # -------------------------------------------------------------
        out_dir = str(self.root / 'clean_test_out')
        sp1 = str(self.root / 'sess1.json')
        sp2 = str(self.root / 'sess2.json')

        # Valid single clean
        assert_valid_in_both({
            'action': 'clean',
            'session_path': sp1,
            'output_dir': out_dir,
            'apply': False,
            'min_age_seconds': 60
        })

        # Valid batch clean
        assert_valid_in_both({
            'action': 'clean',
            'session_paths': [sp1, sp2],
            'output_dir': out_dir,
            'apply': True,
            'min_age_seconds': 0
        })

        # Float integer min_age_seconds accepted and normalized
        req_clean_float_int = {
            'action': 'clean',
            'session_path': sp1,
            'output_dir': out_dir,
            'min_age_seconds': 120.0
        }
        assert_valid_in_both(req_clean_float_int)
        validated_clean = rt.validate(req_clean_float_int.copy())
        self.assertIs(type(validated_clean['min_age_seconds']), int)
        self.assertEqual(validated_clean['min_age_seconds'], 120)

        # Negative Clean 1: Both session_path and session_paths
        assert_invalid_in_both({
            'action': 'clean',
            'session_path': sp1,
            'session_paths': [sp2],
            'output_dir': out_dir
        })

        # Negative Clean 2: Neither session_path nor session_paths
        assert_invalid_in_both({
            'action': 'clean',
            'output_dir': out_dir
        })

        # Negative Clean 3: Empty session_paths array (minItems=1)
        assert_invalid_in_both({
            'action': 'clean',
            'session_paths': [],
            'output_dir': out_dir
        })

        # Negative Clean 4: Duplicate session_paths (uniqueItems=True)
        assert_invalid_in_both({
            'action': 'clean',
            'session_paths': [sp1, sp1],
            'output_dir': out_dir
        })

        # Negative Clean 5: Invalid types (apply string, min_age_seconds float non-int, bool min_age_seconds, negative)
        assert_invalid_in_both({'action': 'clean', 'session_path': sp1, 'output_dir': out_dir, 'apply': 'true'})
        assert_invalid_in_both({'action': 'clean', 'session_path': sp1, 'output_dir': out_dir, 'min_age_seconds': 1.5})
        assert_invalid_in_both({'action': 'clean', 'session_path': sp1, 'output_dir': out_dir, 'min_age_seconds': True})
        assert_invalid_in_both({'action': 'clean', 'session_path': sp1, 'output_dir': out_dir, 'min_age_seconds': -10})

        # -------------------------------------------------------------
        # 2. SESSION: init, load, record_feedback schemas & runtime separation
        # -------------------------------------------------------------
        sess_init_path = self.root / 'phase4b_sess.json'

        # Valid Session Init
        req_sess_init = {
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_init_path),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': 'Phase 4B test brief',
            'keep': ['keep sharpness'],
            'change': ['boost shadows'],
            'session_id': 'phase4b_test_sess',
            'exposure_ev': 0.25,
            'adjustments': {'contrast': 10}
        }
        assert_valid_in_both(req_sess_init)

        # Runtime verification of session_init with initial adjustments & exposure_ev
        init_res = rt.dispatch(req_sess_init.copy())
        self.assertEqual(init_res['revision'], 0)
        self.assertEqual(init_res['session']['revision'], 0)
        self.assertEqual(init_res['session']['history'], [])
        self.assertEqual(init_res['session']['effective_adjustments']['contrast'], 10)
        self.assertEqual(init_res['session']['effective_adjustments']['exposure_ev'], 0.25)
        self.assertTrue(sess_init_path.is_file())

        # Verify subsequent record_feedback inherits initial adjustments and can reset categories
        req_sess_fb = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_init_path),
            'expected_revision': 0,
            'user_feedback': 'Reset contrast and adjust saturation',
            'reset_fields': ['contrast'],
            'adjustments': {'saturation': 15}
        }
        assert_valid_in_both(req_sess_fb)
        fb_res = rt.dispatch(req_sess_fb.copy())
        self.assertEqual(fb_res['revision'], 1)
        self.assertNotIn('contrast', fb_res['effective_adjustments'])
        self.assertEqual(fb_res['effective_adjustments']['exposure_ev'], 0.25)
        self.assertEqual(fb_res['effective_adjustments']['saturation'], 15)

        # Valid Session Load (allows ONLY action, operation, session_path)
        req_sess_load = {
            'action': 'session',
            'operation': 'load',
            'session_path': str(sess_init_path)
        }
        assert_valid_in_both(req_sess_load)
        load_res = rt.dispatch(req_sess_load.copy())
        self.assertEqual(load_res['revision'], 1)

        # Negative Session: extra fields in load
        assert_invalid_in_both({
            'action': 'session',
            'operation': 'load',
            'session_path': str(sess_init_path),
            'source': str(self.source)
        })
        assert_invalid_in_both({
            'action': 'session',
            'operation': 'load',
            'session_path': str(sess_init_path),
            'user_feedback': 'unexpected'
        })

        # Negative Session: extra fields in init (user_feedback, expected_revision, reset_adjustments, reset_fields)
        sess_fail_path = self.root / 'phase4b_sess_fail.json'
        base_init = {
            'action': 'session',
            'operation': 'init',
            'session_path': str(sess_fail_path),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': 'fail test'
        }
        assert_invalid_in_both({**base_init, 'user_feedback': 'not allowed in init'})
        assert_invalid_in_both({**base_init, 'expected_revision': 0})
        assert_invalid_in_both({**base_init, 'reset_adjustments': True})
        assert_invalid_in_both({**base_init, 'reset_fields': ['contrast']})

        # Negative Session: missing required in init
        for req_field in ('source', 'base_profile', 'confirmed_brief', 'session_path'):
            bad = base_init.copy()
            del bad[req_field]
            assert_invalid_in_both(bad)

        # Negative Session: error in init adjustments does not leave half-baked session file
        bad_adj_init = {
            **base_init,
            'adjustments': {'contrast': 999}  # out of bounds [-100, 100]
        }
        self.error('INVALID_REQUEST', rt.dispatch, bad_adj_init)
        self.assertFalse(sess_fail_path.exists(), "Failed session_init must not leave half-baked file on disk")

        # Negative Session: feedback missing expected_revision or passing disallowed fields
        req_fb_base = {
            'action': 'session',
            'operation': 'record_feedback',
            'session_path': str(sess_init_path)
        }
        assert_invalid_in_both(req_fb_base)  # missing expected_revision
        assert_invalid_in_both({**req_fb_base, 'expected_revision': 1, 'source': str(self.source)})
        assert_invalid_in_both({**req_fb_base, 'expected_revision': 1, 'base_profile': str(self.pp3)})
        assert_invalid_in_both({**req_fb_base, 'expected_revision': 1, 'session_id': 'other_id'})

        # Float integer expected_revision in feedback accepted and normalized
        req_fb_float_rev = {
            **req_fb_base,
            'expected_revision': 1.0,
            'user_feedback': 'float rev test'
        }
        assert_valid_in_both(req_fb_float_rev)
        val_fb = rt.validate(req_fb_float_rev.copy())
        self.assertIs(type(val_fb['expected_revision']), int)
        self.assertEqual(val_fb['expected_revision'], 1)

        # -------------------------------------------------------------
        # 3. RAW_QC: ROI validation BEFORE reading source or invoking rawpy
        # -------------------------------------------------------------
        valid_raw_qc = {
            'action': 'raw_qc',
            'source': str(self.source)
        }
        assert_valid_in_both(valid_raw_qc)

        valid_raw_qc_roi = {
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': 10, 'y': 20, 'width': 100, 'height': 200}
        }
        assert_valid_in_both(valid_raw_qc_roi)

        # Float integer normalization in ROI
        raw_qc_float_roi = {
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': 0.0, 'y': 0.0, 'width': 50.0, 'height': 80.0}
        }
        assert_valid_in_both(raw_qc_float_roi)
        norm_roi = rt.validate(raw_qc_float_roi.copy())
        self.assertIs(type(norm_roi['roi']['x']), int)
        self.assertIs(type(norm_roi['roi']['width']), int)
        self.assertEqual(norm_roi['roi']['x'], 0)
        self.assertEqual(norm_roi['roi']['width'], 50)

        # Malformed ROI negative checks
        # 1) missing height
        assert_invalid_in_both({
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': 0, 'y': 0, 'width': 100}
        })
        # 2) unknown field in roi (e.g. 'w')
        assert_invalid_in_both({
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': 0, 'y': 0, 'width': 100, 'height': 100, 'w': 100}
        })
        # 3) non-integer float in roi
        assert_invalid_in_both({
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': 0, 'y': 0, 'width': 100.5, 'height': 100}
        })
        # 4) boolean in roi
        assert_invalid_in_both({
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': True, 'y': 0, 'width': 100, 'height': 100}
        })
        # 5) negative bound in roi
        assert_invalid_in_both({
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': -1, 'y': 0, 'width': 100, 'height': 100}
        })
        # 6) zero width bound in roi
        assert_invalid_in_both({
            'action': 'raw_qc',
            'source': str(self.source),
            'roi': {'x': 0, 'y': 0, 'width': 0, 'height': 100}
        })

        # CRITICAL TEST: Malformed ROI with NON-EXISTENT source must fail with INVALID_REQUEST
        # before reading source or calling rawpy (rawpy call count must remain 0).
        non_existent_source = str(self.root / 'does_not_exist_at_all.nef')
        malformed_roi_req = {
            'action': 'raw_qc',
            'source': non_existent_source,
            'roi': {'x': 0, 'y': 0, 'width': 100}  # missing height
        }
        mock_rawpy = MagicMock()
        with patch.dict('sys.modules', {'rawpy': mock_rawpy}):
            self.error('INVALID_REQUEST', rt.dispatch, malformed_roi_req)
            self.assertEqual(mock_rawpy.imread.call_count, 0,
                             "rawpy.imread must not be called when ROI is malformed")

        # -------------------------------------------------------------
        # 4. Strict numeric & path types (rejection of bool/NaN/inf/strings)
        # -------------------------------------------------------------
        render_base = {
            'action': 'render',
            'source': str(self.source),
            'profile': str(self.pp3),
            'profile_sha256': rt.digest(self.pp3),
            'engine_version': '5.13',
            'output_dir': out_dir,
            'format': 'jpeg'
        }
        # Booleans masquerading as numbers
        assert_invalid_in_both({**render_base, 'exposure_ev': True})
        assert_invalid_in_both({**render_base, 'exposure_ev': False})

        # NaN / Infinity (strict runtime check against non-finite values)
        self.error('INVALID_REQUEST', rt.validate, {**render_base, 'exposure_ev': float('nan')})
        self.error('INVALID_REQUEST', rt.validate, {**render_base, 'exposure_ev': float('inf')})
        self.error('INVALID_REQUEST', rt.validate, {**render_base, 'exposure_ev': float('-inf')})

        # String numbers rejected in both
        assert_invalid_in_both({**render_base, 'exposure_ev': '-0.5'})

        # Float integer accepted in number field
        assert_valid_in_both({**render_base, 'exposure_ev': 1.0})

        # Path type strictness (reject non-strings without TypeError or premature FILE_NOT_FOUND)
        assert_invalid_in_both({**render_base, 'source': 12345})
        assert_invalid_in_both({**render_base, 'source': True})
        assert_invalid_in_both({**render_base, 'source': ''})
        assert_invalid_in_both({**render_base, 'output_dir': 999})
        self.error('INVALID_REQUEST', rt.validate, {**render_base, 'source': '   '})

    def test_phase4d_external_resource_path_anchoring_and_apfs_case_protection(self):
        orig_cwd = os.getcwd()

        # =============================================================
        # 1. Cross-CWD Relative Resource Anchoring (LCP, DCP, ICC)
        # =============================================================
        dir_a = self.root / 'dir_a'
        dir_b = self.root / 'dir_b'
        dir_a.mkdir(parents=True, exist_ok=True)
        dir_b.mkdir(parents=True, exist_ok=True)

        lcp_file = dir_a / 'my_lens.lcp'
        lcp_file.write_bytes(b'fake lcp profile data')
        lcp_sha = rt.digest(lcp_file)

        dcp_file = dir_a / 'my_profile.dcp'
        dcp_file.write_bytes(b'fake dcp profile data')
        dcp_sha = rt.digest(dcp_file)

        icc_file = dir_a / 'my_profile.icc'
        icc_file.write_bytes(b'fake icc profile data')
        icc_sha = rt.digest(icc_file)

        sess_path = dir_a / 'session.json'

        try:
            # Step 1.1: In dir_a, init session with RELATIVE resource paths
            os.chdir(dir_a)
            init_req = {
                'action': 'session',
                'operation': 'init',
                'session_path': str(sess_path),
                'source': str(self.source),
                'base_profile': str(self.pp3),
                'confirmed_brief': 'Relative resource test in dir_a',
                'adjustments': {
                    'lens_correction': {'mode': 'lcp', 'path': 'my_lens.lcp', 'sha256': lcp_sha},
                    'input_profile': {'mode': 'custom', 'path': 'my_profile.dcp', 'sha256': dcp_sha}
                }
            }
            init_res = rt.dispatch(init_req)
            eff_init = init_res['session']['effective_adjustments']
            # Assert paths were anchored to absolute canonical paths in dir_a
            self.assertTrue(os.path.isabs(eff_init['lens_correction']['path']))
            self.assertEqual(eff_init['lens_correction']['path'], str(lcp_file.resolve()))
            self.assertTrue(os.path.isabs(eff_init['input_profile']['path']))
            self.assertEqual(eff_init['input_profile']['path'], str(dcp_file.resolve()))

            # Step 1.2: Switch CWD to dir_b (which has no resource files)
            os.chdir(dir_b)
            self.assertFalse((dir_b / 'my_lens.lcp').exists())
            self.assertFalse((dir_b / 'my_profile.dcp').exists())

            # Session load from dir_b
            load_res = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(sess_path)})
            eff_load = load_res['effective_adjustments']
            self.assertEqual(eff_load['lens_correction']['path'], str(lcp_file.resolve()))
            self.assertEqual(eff_load['input_profile']['path'], str(dcp_file.resolve()))

            # Render from dir_b (session-bound)
            out_dir = self.root / 'render_out_b'
            out_dir.mkdir(parents=True, exist_ok=True)
            render_req = {
                'action': 'render',
                'source': str(self.source),
                'profile': str(self.pp3),
                'profile_sha256': rt.digest(self.pp3),
                'engine_version': '5.13',
                'output_dir': str(out_dir),
                'format': 'jpeg',
                'session_path': str(sess_path),
                'expected_revision': 0
            }
            render_res = self.render_dispatch(render_req)
            self.assertTrue(Path(render_res['output']).is_file())
            # Verify manifest recorded the anchored canonical path from dir_a
            manifest = json.loads(Path(render_res['manifest']).read_text(encoding='utf-8'))
            self.assertEqual(manifest['external_resources']['lens_profile']['canonical_path'], str(lcp_file.resolve()))
            self.assertEqual(manifest['external_resources']['input_profile']['canonical_path'], str(dcp_file.resolve()))

            # Step 1.3: Record feedback from dir_b with an incremental change to another field
            fb_req = {
                'action': 'session',
                'operation': 'record_feedback',
                'session_path': str(sess_path),
                'expected_revision': 1,
                'user_feedback': 'Add contrast, preserve lens and input profile',
                'adjustments': {'contrast': 25}
            }
            fb_res = rt.dispatch(fb_req)
            self.assertEqual(fb_res['revision'], 2)
            eff_fb = fb_res['effective_adjustments']
            # Anchor must be preserved!
            self.assertEqual(eff_fb['lens_correction']['path'], str(lcp_file.resolve()))
            self.assertEqual(eff_fb['input_profile']['path'], str(dcp_file.resolve()))
            self.assertEqual(eff_fb['contrast'], 25)

            # Step 1.4: Update input_profile to ICC profile
            os.chdir(dir_a)
            fb_icc_req = {
                'action': 'session',
                'operation': 'record_feedback',
                'session_path': str(sess_path),
                'expected_revision': 2,
                'user_feedback': 'Switch to ICC',
                'adjustments': {'input_profile': {'mode': 'custom', 'path': 'my_profile.icc', 'sha256': icc_sha}}
            }
            fb_icc_res = rt.dispatch(fb_icc_req)
            self.assertEqual(fb_icc_res['effective_adjustments']['input_profile']['path'], str(icc_file.resolve()))

            # Switch back to dir_b and verify render still succeeds with ICC profile
            os.chdir(dir_b)
            render_icc_req = {
                **render_req,
                'expected_revision': 3
            }
            render_icc_res = self.render_dispatch(render_icc_req)
            manifest_icc = json.loads(Path(render_icc_res['manifest']).read_text(encoding='utf-8'))
            self.assertEqual(manifest_icc['external_resources']['input_profile']['canonical_path'], str(icc_file.resolve()))

        finally:
            os.chdir(orig_cwd)

        # =============================================================
        # 2. APFS Case-Insensitive Protection in clean
        # =============================================================
        clean_out = self.root / 'clean_case_out'
        clean_out.mkdir(parents=True, exist_ok=True)

        # Candidate directory on disk with UPPERCASE
        orphan_upper = clean_out / 'ORPHAN_UPPER__rt-5.13__tag1-111'
        orphan_upper.mkdir(parents=True, exist_ok=True)
        (orphan_upper / '.active.lock').touch()
        real_lens_upper = orphan_upper / 'REAL_LENS.lcp'
        real_lens_upper.write_bytes(b'lens data in uppercase dir')
        real_lens_sha = rt.digest(real_lens_upper)

        (orphan_upper / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': 'case_test_sess',
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # Prefix orphan (same prefix but separate directory - MUST be deleted)
        orphan_prefix = clean_out / 'ORPHAN_UPPER__rt-5.13__tag1-111_PREFIX'
        orphan_prefix.mkdir(parents=True, exist_ok=True)
        (orphan_prefix / '.active.lock').touch()
        (orphan_prefix / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': 'case_test_sess',
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # Pure orphan (completely unreferenced - MUST be deleted)
        orphan_pure = clean_out / 'ORPHAN_PURE__rt-5.13__tag2-222'
        orphan_pure.mkdir(parents=True, exist_ok=True)
        (orphan_pure / '.active.lock').touch()
        (orphan_pure / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': 'case_test_sess',
            'source_sha256': rt.digest(self.source), 'profile': {'sha256': rt.digest(self.pp3)}
        }), encoding='utf-8')

        # Session file references the file via lowercase path alias
        case_alias_path = clean_out / 'orphan_upper__rt-5.13__tag1-111' / 'real_lens.lcp'
        case_sess_file = self.root / 'case_sess.json'
        rt.dispatch({
            'action': 'session',
            'operation': 'init',
            'session_path': str(case_sess_file),
            'source': str(self.source),
            'base_profile': str(self.pp3),
            'confirmed_brief': 'Case test brief',
            'session_id': 'case_test_sess',
            'adjustments': {
                'lens_correction': {'mode': 'lcp', 'path': str(case_alias_path), 'sha256': real_lens_sha}
            }
        })

        # Dry-run clean
        res_dry = rt.dispatch({
            'action': 'clean',
            'session_path': str(case_sess_file),
            'output_dir': str(clean_out),
            'apply': False,
            'min_age_seconds': 0
        })
        dry_skipped = {Path(s['path']).name: s['reason'] for s in res_dry['skipped']}
        self.assertEqual(dry_skipped.get(orphan_upper.name), 'protected_user_file_inside',
                         "APFS case alias must protect candidate directory in dry-run")

        # Apply clean
        res_apply = rt.dispatch({
            'action': 'clean',
            'session_path': str(case_sess_file),
            'output_dir': str(clean_out),
            'apply': True,
            'min_age_seconds': 0
        })
        apply_skipped = {Path(s['path']).name: s['reason'] for s in res_apply['skipped']}
        self.assertEqual(apply_skipped.get(orphan_upper.name), 'protected_user_file_inside',
                         "APFS case alias must protect candidate directory in apply clean")
        self.assertTrue(orphan_upper.exists(), "Directory with case-aliased resource must NOT be deleted")

        # Verify prefix orphan and pure orphan were actually deleted!
        self.assertFalse(orphan_prefix.exists(), "Directory with only matching prefix must be deleted")
        self.assertFalse(orphan_pure.exists(), "Unreferenced pure orphan must be deleted")

        # =============================================================
        # 3. Corrupt relative session rejection & reset recovery
        # =============================================================
        corrupt_sess_file = self.root / 'unanchored_sess.json'
        unanchored_data = {
            'schema_version': 1,
            'session_id': 'unanchored_sess',
            'source': {'path': str(self.source), 'sha256': rt.digest(self.source), 'source_type': 'raw'},
            'base_profile': {'path': str(self.pp3), 'sha256': rt.digest(self.pp3)},
            'confirmed_brief': 'Unanchored test',
            'keep': [],
            'change': [],
            'revision': 0,
            'effective_adjustments': {
                'lens_correction': {'mode': 'lcp', 'path': 'unanchored_rel_lens.lcp', 'sha256': '0'*64}
            },
            'history': []
        }
        corrupt_sess_file.write_text(json.dumps(unanchored_data), encoding='utf-8')

        # session_load rejects unanchored session
        self.error('CORRUPT_SESSION', rt.dispatch, {
            'action': 'session', 'operation': 'load', 'session_path': str(corrupt_sess_file)
        })

        # clean rejects unanchored session
        self.error('CORRUPT_SESSION', rt.dispatch, {
            'action': 'clean', 'session_path': str(corrupt_sess_file), 'output_dir': str(clean_out)
        })

        # Checksum mismatch on file modification & reset recovery
        # Modify lcp_file content in dir_a
        lcp_file.write_bytes(b'tampered lcp data')
        self.error('CHECKSUM_MISMATCH', rt.dispatch, {
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_path), 'expected_revision': 4,
            'user_feedback': 'Attempt feedback with tampered file',
            'adjustments': {'contrast': 5}
        })

        # Recovery using reset_fields: ['lens_correction']
        reset_res = rt.dispatch({
            'action': 'session', 'operation': 'record_feedback',
            'session_path': str(sess_path), 'expected_revision': 4,
            'user_feedback': 'Reset broken lens_correction',
            'reset_fields': ['lens_correction']
        })
        self.assertEqual(reset_res['revision'], 5)
        self.assertNotIn('lens_correction', reset_res['effective_adjustments'])

    def test_incremental_schema_field_contract(self):
        validator = jsonschema.Draft202012Validator(rt.schema())
        jsonschema.Draft202012Validator.check_schema(rt.schema())
        good = [
            {'white_balance': {'temperature': 5200}},
            {'demosaic': {'method': 'amazevng4', 'dual_contrast': 20.5}},
            {'raw_preprocess': {'green_equilibration': 15, 'line_noise': 120,
                                'hot_dead_pixel_threshold': 100, 'hot_pixel_filter': True}},
            {'raw_calibration': {'black_offsets': {'red': 200}}},
            {'input_profile': {'dcp_illuminant': 1}},
            {'tone_curve': {'points': [[0, 0], [1, 1]]}}
        ]
        bad = [
            {'raw_preprocess': {'green_equilibration': True}},
            {'raw_preprocess': {'line_noise': False}},
            {'raw_preprocess': {'hot_dead_pixel_thresh': 40}},
            {'raw_preprocess': {'hot_dead_pixel_threshold': 19}},
            {'raw_preprocess': {'hot_dead_pixel_threshold': 201}},
            {'raw_preprocess': {'green_equilibration': 101}},
            {'raw_preprocess': {'line_noise': 1001}},
            {'demosaic': {'dual_contrast': 20, 'dual_contrast_auto': True}},
            {'demosaic': {'dual_contrast': True}},
            {'white_balance': {'temperature': 5200.5}},
            {'raw_calibration': {'black_offsets': {'red': True}}},
            {'input_profile': {'dcp_illuminant': 3}},
            {'tone_curve': {'points': [[0, 0], [1, 2]]}},
            {'unknown_adjustment': 1}
        ]
        for adjustments in good + bad:
            with self.subTest(adjustments=adjustments):
                req = {**self.request, 'adjustments': adjustments}
                expected = adjustments in good
                self.assertEqual(validator.is_valid(req), expected)
                if expected:
                    rt.validate(req)
                else:
                    self.error('INVALID_REQUEST', rt.validate, req)
                    # Invalid shape must fail before attempting source IO or engine startup.
                    self.error('INVALID_REQUEST', rt.dispatch, {**req, 'source': '/missing.NEF'})

    def test_manual_threshold_session_lifecycle(self):
        session = self.root / 'manual_session.json'
        initial = {'demosaic': {'method': 'amazevng4', 'dual_contrast': 20.5},
                   'contrast': 10, 'raw_calibration': {'black_offsets': {'red': 0}}}
        original = json.loads(json.dumps(initial))
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3),
                     'confirmed_brief': 'Manual threshold lifecycle', 'adjustments': initial})
        self.assertEqual(initial, original, 'Normalization must not mutate the caller patch')
        load = lambda: rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(session)})
        self.assertFalse(load()['effective_adjustments']['demosaic']['dual_contrast_auto'])

        def feedback(revision, adjustments):
            return rt.dispatch({'action': 'session', 'operation': 'record_feedback',
                                'session_path': str(session), 'expected_revision': revision,
                                'adjustments': adjustments})
        auto = feedback(0, {'demosaic': {'dual_contrast_auto': True}})
        self.assertEqual(auto['effective_adjustments']['demosaic'],
                         {'method': 'amazevng4', 'dual_contrast_auto': True})
        manual = feedback(1, {'demosaic': {'dual_contrast': 30.5}})
        self.assertFalse(manual['effective_adjustments']['demosaic']['dual_contrast_auto'])
        before = session.read_bytes()
        self.error('INVALID_REQUEST', feedback, 2, {'demosaic': {'dual_contrast': 40, 'dual_contrast_auto': True}})
        self.assertEqual(session.read_bytes(), before)
        output = self.render_dispatch({**self.request, 'exposure_ev': 0,
                                       'session_path': str(session), 'expected_revision': 2})
        manifest = json.loads(Path(output['manifest']).read_text())
        self.assertFalse(manifest['effective_adjustments']['demosaic']['dual_contrast_auto'])
        pp3 = (Path(output['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('DualDemosaicContrast=30.5', pp3)
        self.assertIn('PreBlack1=0', pp3)
        auto = feedback(3, {'demosaic': {'dual_contrast_auto': True}})
        output = self.render_dispatch({**self.request, 'session_path': str(session), 'expected_revision': 4})
        pp3 = (Path(output['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('DualDemosaicAutoContrast=true', pp3)
        self.assertNotIn('DualDemosaicContrast=', pp3)
        self.assertEqual(auto['effective_adjustments']['contrast'], 10)
        self.assertEqual(auto['effective_adjustments']['raw_calibration']['black_offsets']['red'], 0)

    def test_missing_test_dependencies_fail_before_suite(self):
        proc = subprocess.run([sys.executable, '-S', str(Path(__file__).resolve())],
                              capture_output=True, text=True, timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('TEST_DEPENDENCY_MISSING (no tests ran)', proc.stderr)
        self.assertIn('jsonschema', proc.stderr)
        self.assertIn('uv run --script', proc.stderr)
        self.assertNotIn('Traceback', proc.stderr)
        self.assertNotIn('Ran ', proc.stderr)

    def test_documented_requests_validate_and_preprocess_renders(self):
        import re
        validator = jsonschema.Draft202012Validator(rt.schema())
        root = Path(__file__).parents[1]
        documents = [root / 'README.md', *sorted((root / 'references').glob('*.md'))]
        saw_preprocess = False
        for doc in documents:
            for block in re.findall(r'```json\s*\n(.*?)\n```', doc.read_text(), flags=re.S):
                req = json.loads(block)
                if not isinstance(req, dict) or 'action' not in req:
                    continue
                with self.subTest(doc=doc.name, action=req['action']):
                    validator.validate(req)
                    rt.validate(req)
                    if 'raw_preprocess' in req.get('adjustments', {}):
                        saw_preprocess = True
                        result = self.render_dispatch({**self.request, 'adjustments': req['adjustments']})
                        pp3 = (Path(result['manifest']).parent / 'effective.pp3').read_text()
                        for key in ['HotPixelFilter=true', 'DeadPixelFilter=true',
                                    'HotDeadPixelThresh=100', 'LineDenoise=120',
                                    'LineDenoiseDirection=3', 'GreenEqThreshold=15',
                                    'DualDemosaicAutoContrast=false', 'DualDemosaicContrast=20.5']:
                            self.assertIn(key, pp3)
        self.assertTrue(saw_preprocess, 'Documented preprocessing request must be exercised')

    def test_selected_old_render_restore_drops_later_exploration(self):
        session_path = str(self.root / 'session.json')
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': session_path,
                     'source': str(self.source), 'base_profile': str(self.pp3),
                     'confirmed_brief': '保留暖光，树皮纹理清楚', 'keep': ['现场暖光']})
        bound = {**self.request, 'session_path': session_path}
        chosen = self.render_dispatch({**bound, 'expected_revision': 0})
        snapshot = json.loads(Path(chosen['manifest']).read_text())['effective_adjustments']
        self.render_dispatch({**bound, 'expected_revision': 1,
                              'adjustments': {'saturation': 25}})
        brief = '改成弱细节，夜色优先；用户选中版：' + chosen['manifest']
        rt.dispatch({'action': 'session', 'operation': 'record_feedback',
                     'session_path': session_path, 'expected_revision': 2,
                     'user_feedback': '回到第一版继续，保留暖光和这个反差，细节少点也行，夜色优先。',
                     'confirmed_brief': brief, 'keep': ['现场暖光', '选中版反差'],
                     'change': ['在夜色优先前提下调整细节'],
                     'reset_adjustments': True, 'adjustments': snapshot})
        loaded = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': session_path})
        self.assertEqual(loaded['effective_adjustments'], snapshot)
        self.assertNotIn('saturation', loaded['effective_adjustments'])
        self.assertEqual(loaded['confirmed_brief'], brief)
        self.assertEqual(loaded['keep'], ['现场暖光', '选中版反差'])
        self.assertEqual(loaded['revision'], 3)
        self.assertTrue(Path(chosen['output']).is_file())

    def test_readme_requests_run_through_cli_session_flow(self):
        import re
        from copy import deepcopy
        root = Path(__file__).parents[1]
        requests = [json.loads(block) for block in re.findall(
            r'```json\s*\n(.*?)\n```', (root / 'README.md').read_text(), flags=re.S)]
        def example(action, operation=None):
            return deepcopy(next(req for req in requests
                                 if req['action'] == action and req.get('operation') == operation))

        def cli(req, via_file=False):
            if via_file:
                request_file = self.root / 'request with spaces.json'
                request_file.write_text(json.dumps(req), encoding='utf-8')
                args, data = [str(request_file)], None
            else:
                args, data = ['-'], json.dumps(req)
            proc = subprocess.run([sys.executable, str(root / 'scripts/rt_tool.py'), '--request', *args],
                                  input=data, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            response = json.loads(proc.stdout)
            self.assertTrue(response['ok'])
            return response['result']

        profile = cli(example('profile'))
        init = example('session', 'init')
        init.update(source=str(self.source), session_path=str(self.root / 'session.json'))
        created = cli(init, via_file=True)
        render = example('render')
        render.update(source=init['source'], session_path=init['session_path'],
                      profile_sha256=profile['sha256'], output_dir=str(self.root / 'output'),
                      expected_revision=created['revision'])
        result = self.render_dispatch(render)
        self.assertTrue(Path(result['output']).is_file())
        feedback = example('session', 'record_feedback')
        feedback['session_path'] = init['session_path']
        updated = cli(feedback)
        load = example('session', 'load')
        load['session_path'] = init['session_path']
        restored = cli(load, via_file=True)
        self.assertEqual(restored['revision'], updated['revision'])
        self.assertEqual(restored['revision'], 2)
        self.assertEqual(restored['confirmed_brief'], init['confirmed_brief'])
        self.assertEqual(restored['keep'], feedback['keep'])
        self.assertTrue(set(init['keep']) <= set(restored['keep']))
        self.assertEqual(restored['effective_adjustments'], render['adjustments'])
        self.assertEqual(restored['session']['history'][-1]['user_feedback'], feedback['user_feedback'])

    def test_dual_to_single_transition_discards_only_inherited_controls(self):
        for method in rt.DUAL_DEMOSAIC_METHODS:
            for control in [{'dual_contrast_auto': True}, {'dual_contrast_auto': False, 'dual_contrast': 20}]:
                base = {'demosaic': {'method': method, **control}, 'contrast': 15}
                resolved = rt.resolve_effective_adjustments(base, {'adjustments': {'demosaic': {'method': 'AMAZE'}}}, '5.13')
                self.assertEqual(resolved, {'demosaic': {'method': 'amaze'}, 'contrast': 15})
                self.assertIn('dual_contrast_auto', base['demosaic'])
        session = self.root / 'switch.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Switch method',
                     'adjustments': {'demosaic': {'method': 'amazebilinear', 'dual_contrast': 20}, 'saturation': 5}})
        changed = rt.dispatch({'action': 'session', 'operation': 'record_feedback', 'session_path': str(session),
                              'expected_revision': 0, 'adjustments': {'demosaic': {'method': 'rcd'}}})
        self.assertEqual(changed['effective_adjustments']['demosaic'], {'method': 'rcd'})
        rendered = self.render_dispatch({**self.request, 'session_path': str(session), 'expected_revision': 1})
        manifest = json.loads(Path(rendered['manifest']).read_text())
        self.assertEqual(manifest['effective_adjustments']['demosaic'], {'method': 'rcd'})
        pp3 = (Path(rendered['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('Method=rcd', pp3)
        self.assertNotIn('DualDemosaic', pp3)
        self.assertEqual(manifest['effective_adjustments']['saturation'], 5)
        before = session.read_bytes()
        for explicit in [{'dual_contrast': 20}, {'dual_contrast_auto': False}]:
            self.error('INVALID_REQUEST', rt.dispatch, {
                'action': 'session', 'operation': 'record_feedback', 'session_path': str(session),
                'expected_revision': 2, 'adjustments': {'demosaic': {'method': 'amaze', **explicit}}})
            self.assertEqual(session.read_bytes(), before)
        # Switching back to dual starts without resurrecting its former threshold.
        back = rt.dispatch({'action': 'session', 'operation': 'record_feedback', 'session_path': str(session),
                            'expected_revision': 2, 'adjustments': {'demosaic': {'method': 'amazebilinear'}}})
        self.assertEqual(back['effective_adjustments']['demosaic'], {'method': 'amazebilinear'})

    def test_integer_and_enum_normalization_across_entry_points(self):
        adj = {'contrast': 15.0, 'saturation': 5.0,
               'white_balance': {'temperature': 5500.0, 'green': 1.05},
               'sharpen': {'amount': 200.0},
               'highlight_recovery': {'enabled': True, 'method': 'CoLoRoPp'},
               'highlight_compression': {'amount': 30.0, 'threshold': 10.0},
               'demosaic': {'method': 'AmAzEbIlInEaR'},
               'raw_preprocess': {'hot_dead_pixel_threshold': 100.0, 'line_noise': 120.0,
                                  'green_equilibration': 15.0, 'line_noise_direction': 'BoTh'},
               'input_profile': {'mode': 'AuTo_MaTcHeD', 'dcp_illuminant': 1.0},
               'lens_correction': {'mode': 'NoNe'},
               'tone_curve': {'mode': 'fIlMlIkE', 'points': [[0, 0], [1, 1]]}}
        original = json.loads(json.dumps(adj))
        validator = jsonschema.Draft202012Validator(rt.schema())
        validator.validate({**self.request, 'adjustments': adj})
        rt.validate_adjustments_patch(adj, '5.13')
        self.assertIs(type(adj['contrast']), int)
        self.assertIs(type(adj['white_balance']['temperature']), int)
        self.assertIs(type(adj['input_profile']['dcp_illuminant']), int)
        result = self.render_dispatch({**self.request, 'adjustments': original})
        effective = json.loads(Path(result['manifest']).read_text())['effective_adjustments']
        self.assertEqual(effective['tone_curve']['mode'], 'FilmLike')
        self.assertEqual(effective['demosaic']['method'], 'amazebilinear')
        self.assertEqual(effective['input_profile']['mode'], 'auto_matched')
        pp3 = (Path(result['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('CurveMode=FilmLike', pp3)
        self.assertIn('Temperature=5500', pp3)
        for value in [15.5, True, '15', float('nan'), float('inf')]:
            self.error('INVALID_REQUEST', rt.validate_adjustments_patch, {'contrast': value}, '5.13')
        for mode in ['filmlikee', '', 'FilmLike\n']:
            bad = {'tone_curve': {'mode': mode, 'points': [[0, 0], [1, 1]]}}
            self.error('INVALID_REQUEST', rt.validate_adjustments_patch, bad, '5.13')

    def test_reset_then_replacement_exposure_contract(self):
        base = {'exposure_ev': 1.2, 'contrast': 10}
        for req in [{'reset_fields': ['exposure_ev'], 'exposure_ev': 0.8},
                    {'reset_fields': ['exposure_ev'], 'adjustments': {'exposure_ev': 0.8}}]:
            self.assertEqual(rt.resolve_effective_adjustments(base, req, '5.13'),
                             {'contrast': 10, 'exposure_ev': 0.8})
        self.assertEqual(rt.resolve_effective_adjustments(base, {'reset_fields': ['exposure_ev']}, '5.13'), {'contrast': 10})
        self.error('INVALID_REQUEST', rt.resolve_effective_adjustments, base,
                   {'exposure_ev': 0.8, 'adjustments': {'exposure_ev': 0.8}}, '5.13')

    def test_builtin_profile_cross_cwd_and_session_binding(self):
        alias = 'builtin:neutral-rec2020-rt513'
        real = Path(__file__).resolve().parents[1] / 'assets/neutral-rec2020-rt513.pp3'
        shadow = self.root / 'assets/neutral-rec2020-rt513.pp3'
        shadow.parent.mkdir()
        shadow.write_text(PP3 + '# project-owned shadow profile\n')
        script = Path(rt.__file__).resolve()
        def cli(req):
            result = subprocess.run([sys.executable, str(script), '--request', '-'],
                                    cwd=self.root, input=json.dumps(req), text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)['result']
        builtin = cli({'action': 'profile', 'profile': alias})
        self.assertEqual(builtin['path'], str(real))
        self.assertEqual(builtin['sha256'], rt.digest(real))
        relative = cli({'action': 'profile', 'profile': 'assets/neutral-rec2020-rt513.pp3'})
        self.assertEqual(relative['path'], str(shadow))
        self.assertNotEqual(relative['sha256'], builtin['sha256'])
        for invalid in ['builtin:missing', 'builtin:../assets/neutral-rec2020-rt513.pp3']:
            self.error('INVALID_REQUEST', rt.profile, invalid)
        self.error('INVALID_REQUEST', rt.profile, alias, 'lcp')
        session = self.root / 'builtin-session.json'
        initialized = cli({'action': 'session', 'operation': 'init', 'session_path': str(session),
                           'source': str(self.source), 'base_profile': alias, 'confirmed_brief': 'Builtin profile'})
        self.assertEqual(initialized['session']['base_profile']['path'], str(real))
        result = self.render_dispatch({**self.request, 'profile': alias, 'profile_sha256': builtin['sha256'],
                                       'session_path': str(session), 'expected_revision': 0})
        self.assertTrue(Path(result['output']).is_file())

    def test_clean_preflight_collects_errors_and_never_deletes(self):
        out = self.root / 'preflight-output'
        out.mkdir()
        sessions = []
        for idx in range(2):
            session = self.root / f'preflight-{idx}.json'
            rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                         'source': str(self.source), 'base_profile': str(self.pp3),
                         'confirmed_brief': 'Preflight', 'session_id': f'preflight-{idx}'})
            sessions.append(str(session))
        orphan = out / 'orphan'
        orphan.mkdir()
        (orphan / '.active.lock').touch()
        (orphan / 'image.jpg').write_bytes(b'temporary output')
        (orphan / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'cleanup_protocol_version': 1, 'tool_version': rt.VERSION,
            'session_id': 'preflight-0', 'source_sha256': rt.digest(self.source),
            'profile': {'sha256': rt.digest(self.pp3)}}))
        broken = self.root / 'corrupt.json'
        broken.write_text('{broken')
        absent = str(self.root / 'missing.json')
        req = {'action': 'clean', 'session_paths': [sessions[0], absent, str(broken), sessions[1]],
               'output_dir': str(out), 'min_age_seconds': 0, 'preflight_only': True}
        result = rt.dispatch(req)
        self.assertEqual((result['succeeded'], result['failed'], result['status']), (2, 2, 'partial'))
        self.assertEqual(result['deleted_count'], 0)
        self.assertFalse(result['normal_clean_precheck_passed'])
        self.assertEqual([s['error']['code'] for s in result['sessions'] if not s['ok']],
                         ['FILE_NOT_FOUND', 'CORRUPT_SESSION'])
        self.assertTrue(orphan.exists())
        validator = jsonschema.Draft202012Validator(rt.schema())
        validator.validate(req)
        forbidden = {**req, 'apply': True}
        self.assertFalse(validator.is_valid(forbidden))
        self.error('INVALID_REQUEST', rt.dispatch, forbidden)
        self.error('FILE_NOT_FOUND', rt.dispatch, {**req, 'preflight_only': False, 'apply': True})
        self.assertTrue(orphan.exists())
        malformed = self.root / 'malformed-shape.json'
        bad_shape = json.loads(Path(sessions[0]).read_text())
        bad_shape['source'] = None
        malformed.write_text(json.dumps(bad_shape))
        malformed_result = rt.dispatch({**req, 'session_paths': [str(malformed), sessions[1]]})
        self.assertEqual((malformed_result['succeeded'], malformed_result['failed']), (1, 1))
        self.assertEqual(malformed_result['sessions'][0]['error']['code'], 'PREFLIGHT_FAILED')
        self.assertTrue(orphan.exists())
        clone = self.root / 'cloned-session.json'
        clone.write_bytes(Path(sessions[0]).read_bytes())
        duplicated = rt.dispatch({**req, 'session_paths': [sessions[0], str(clone)]})
        self.assertEqual(duplicated['failed'], 1)
        self.assertFalse(duplicated['normal_clean_precheck_passed'])
        self.assertEqual(duplicated['sessions'][1]['error']['code'], 'INVALID_REQUEST')
        # Fixing the supplied session list makes the normal multi-session operation usable.
        ready = rt.dispatch({**req, 'session_paths': sessions})
        self.assertEqual(ready['status'], 'complete')
        self.assertTrue(ready['normal_clean_precheck_passed'])
        cleaned = rt.dispatch({**req, 'session_paths': sessions, 'preflight_only': False, 'apply': True})
        self.assertEqual(cleaned['total_orphans'], 1)
        self.assertFalse(orphan.exists())

    def test_compact_and_selected_capabilities(self):
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}):
            full = rt.dispatch({'action': 'capabilities'})
            summary = rt.dispatch({'action': 'capabilities', 'source_type': 'raw', 'detail': 'summary'})
            selected = rt.dispatch({'action': 'capabilities', 'detail': 'full', 'parameters': ['white_balance', 'tone_curve']})
            self.assertNotIn('controllable_parameters', summary)
            self.assertEqual(set(summary['available_adjustments']), set(full['controllable_parameters']))
            self.assertEqual(set(selected['controllable_parameters']), {'white_balance', 'tone_curve'})
            self.assertLess(len(json.dumps(summary)), len(json.dumps(full)) / 3)
            self.assertEqual(summary['runtime_limits']['recommended_concurrent_renders'], 1)
            self.assertFalse(summary['runtime_limits']['cross_session_throttle_enforced'])
            jpeg = rt.dispatch({'action': 'capabilities', 'source_type': 'jpeg', 'detail': 'summary'})
            self.assertTrue(set(jpeg['available_adjustments']).isdisjoint(rt.RAW_ONLY_ADJUSTMENTS))
            self.error('UNSUPPORTED_ADJUSTMENTS', rt.dispatch, {'action': 'capabilities', 'source_type': 'jpeg', 'parameters': ['demosaic']})
            for parameters in [[], ['contrast', 'contrast'], ['invented'], [{}]]:
                self.error('INVALID_REQUEST', rt.dispatch, {'action': 'capabilities', 'parameters': parameters})
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.12'}):
            self.assertFalse(rt.dispatch({'action': 'capabilities', 'detail': 'summary'})['structured_adjustments_available'])

    def test_refactored_patch_completeness_only_after_inheritance(self):
        base = {'denoise': {'enabled': False, 'luma': 20, 'chroma': 15},
                'sharpen': {'enabled': False, 'amount': 120, 'radius': 0.5}}
        patch_only = {'denoise': {'enabled': True}, 'sharpen': {'enabled': True}}
        rt.validate_adjustments_patch(patch_only, '5.13')
        resolved = rt.resolve_effective_adjustments(base, {'adjustments': patch_only}, '5.13')
        self.assertEqual(resolved['denoise'], {'enabled': True, 'luma': 20, 'chroma': 15})
        self.assertEqual(resolved['sharpen'], {'enabled': True, 'amount': 120, 'radius': 0.5})
        self.error('INVALID_REQUEST', rt.resolve_effective_adjustments, {}, {'adjustments': patch_only}, '5.13')
        self.error('UNSUPPORTED_ADJUSTMENTS', rt.validate_adjustments_patch, {'contrast': 1}, '5.12')
        for bad in [
            {'lens_correction': {'camera_model': 'camera\nInjected=value'}},
            {'input_profile': {'path': '/tmp/test.txt'}},
            {'lens_correction': {'path': '/tmp/test.icc'}},
            {'lens_correction': {'mode': 'lcp', 'path': '/missing/profile.lcp', 'sha256': '0'*64, 'camera_make': 'Nikon'}},
            {'input_profile': {'mode': 'camera_standard', 'path': '/missing/profile.dcp', 'sha256': '0'*64}},
            {'raw_calibration': {'black_offsets': {'red': float('inf')}}},
            {'tone_curve': {'points': [[0, 0], [0.5, 0.7], [1, 0.6]]}}
        ]:
            self.error('INVALID_REQUEST', rt.validate_adjustments_patch, bad, '5.13')
            self.error('INVALID_REQUEST', rt.resolve_effective_adjustments, {}, {'adjustments': bad}, '5.13')

    def test_preview_geometry_color_and_source_integrity(self):
        source = self.root / '全尺寸导出.jpg'
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        im = Image.new('RGB', (4000, 2000), (20, 120, 200))
        exif = im.getexif()
        exif[0x0112] = 6
        im.save(source, 'JPEG', icc_profile=icc, exif=exif)
        before = rt.digest(source)
        target = self.root / '预览.jpg'
        stats = rt.qc(source, preview_path=target)
        self.assertEqual((stats['width'], stats['height']), (4000, 2000))
        self.assertEqual(rt.digest(source), before)
        info = stats['preview']
        self.assertEqual((info['width'], info['height']), (800, 1600))
        self.assertEqual(info['sha256'], rt.digest(target))
        with Image.open(target) as preview:
            self.assertEqual(preview.mode, 'RGB')
            self.assertEqual(preview.size, (800, 1600))
            self.assertIn('sRGB', ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(rt.io.BytesIO(preview.info['icc_profile']))))
            self.assertNotEqual(preview.getexif().get(0x0112), 6)
        small = self.root / 'small.jpg'
        Image.new('RGB', (20, 10)).save(small, icc_profile=icc)
        self.assertEqual(rt.qc(small, preview_path=self.root / 'small-preview.jpg')['preview']['width'], 20)

    def test_preview_manifest_and_session_history(self):
        session = self.root / 'preview-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Preview'})
        rendered = self.render_dispatch({**self.request, 'session_path': str(session), 'expected_revision': 0})
        manifest = json.loads(Path(rendered['manifest']).read_text())
        history = json.loads(session.read_text())['history'][-1]
        self.assertEqual(manifest['preview'], rendered['preview'])
        self.assertEqual(Path(rendered['preview']['path']).name, 'preview.jpg')
        self.assertEqual(history['preview_sha256'], rt.digest(rendered['preview']['path']))
        self.assertEqual(history['preview_path'], rendered['preview']['path'])
        self.assertEqual(manifest['output_sha256'], rt.digest(rendered['output']))
        before = session.read_bytes()
        with patch.object(rt, 'write_preview', side_effect=rt.ToolError('PREVIEW_FAILED', 'synthetic failure')):
            self.error('PREVIEW_FAILED', self.render_dispatch,
                       {**self.request, 'session_path': str(session), 'expected_revision': 1})
        self.assertEqual(session.read_bytes(), before)

    def test_lens_database_name_evidence_is_not_engine_verification(self):
        contents = self.root / 'TestRT.app/Contents'
        database = contents / 'Resources/share/lensfun'
        database.mkdir(parents=True)
        (database / 'lenses.xml').write_text('''<lensdatabase><lens><maker>Example</maker>
            <model>AF 50/2 Z</model><mount>Nikon Z</mount>
            <calibration><distortion model="ptlens"/></calibration></lens></lensdatabase>''')
        eng = {'path': str(contents / 'MacOS/rawtherapee-cli'), 'version': '5.13'}
        with patch.object(rt, 'engine', return_value=eng):
            found = rt.lens_support_check([{'LensModel': 'Example AF 50/2 Z'}])
            self.assertEqual(found['status'], 'exact_name_candidate')
            self.assertTrue(found['candidates'][0]['calibration_entries']['distortion'])
            self.assertFalse(found['candidates'][0]['calibration_entries']['vignetting'])
            for key in ('engine_database_verified', 'optical_correction_verified', 'mount_compatibility_verified'):
                self.assertFalse(found[key])
            self.assertEqual(found['engine_match_status'], 'unknown')
            missing = rt.lens_support_check([{'LensModel': 'Viltrox AF 50/2.0 Z'}])
            self.assertEqual(missing['status'], 'not_found_in_scanned_database')
            (database / 'broken.xml').write_text('<broken')
            incomplete = rt.lens_support_check([{'LensModel': 'unlisted'}])
            self.assertEqual(incomplete['status'], 'incomplete_scan')
            self.assertEqual(incomplete['parse_error_count'], 1)
        with patch.object(rt, 'engine') as mocked:
            self.assertEqual(rt.lens_support_check([])['status'], 'missing_metadata')
            mocked.assert_not_called()
        with patch.object(rt, 'engine', return_value={'path': '/missing/rt'}):
            self.assertEqual(rt.lens_support_check([{'LensModel': 'unlisted'}])['status'], 'database_unavailable')

    def test_inspect_lens_opt_in_and_render_warning(self):
        with patch.object(rt.shutil, 'which', return_value='/fake/exiftool'), patch.object(rt, 'run', return_value='[{"LensModel":"unlisted"}]'), patch.object(rt, 'lens_support_check', return_value={'status': 'not_found_in_scanned_database', 'hint': 'Not an engine match'}) as checked:
            plain = rt.dispatch({'action': 'inspect', 'source': str(self.source)})
            self.assertNotIn('lens_support', plain)
            checked.assert_not_called()
            diagnostic = rt.dispatch({'action': 'inspect', 'source': str(self.source), 'lens_check': True})
            self.assertEqual(diagnostic['lens_support_hint'], 'Not an engine match')
            checked.assert_called_once()
        self.error('INVALID_REQUEST', rt.dispatch, {'action': 'inspect', 'source': '/missing.NEF', 'lens_check': 'true'})
        rendered = self.render_dispatch({**self.request, 'adjustments': {'lens_correction': {'mode': 'lensfun_auto'}}})
        self.assertEqual(rendered['warnings'][0]['code'], 'LENSFUN_MATCH_UNVERIFIED')
        manifest = json.loads(Path(rendered['manifest']).read_text())
        self.assertEqual(manifest['warnings'], rendered['warnings'])
        self.assertFalse(manifest['verification']['lens_correction']['effect_verified'])

    def test_disk_preflight_boundaries_and_gate_release(self):
        for free in [rt.MIN_RENDER_FREE_BYTES, rt.MIN_RENDER_FREE_BYTES + 1]:
            with patch.object(rt.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=free)):
                self.assertEqual(rt.check_render_space(self.root / 'new/child')['free_bytes'], free)
        with patch.object(rt.shutil, 'disk_usage', side_effect=OSError('probe failed')):
            self.error('DISK_CHECK_FAILED', rt.check_render_space, self.root)
        session = self.root / 'disk-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Disk preflight'})
        before = session.read_bytes()
        req = {**self.request, 'session_path': str(session), 'expected_revision': 0}
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=rt.MIN_RENDER_FREE_BYTES - 1)), patch.object(rt, 'run') as runner:
            self.error('INSUFFICIENT_DISK_SPACE', rt.dispatch, req)
            runner.assert_not_called()
        self.assertFalse(Path(req['output_dir']).exists())
        self.assertEqual(session.read_bytes(), before)
        gate = rt.FileLock(Path(str(session) + '.render.gate'))
        self.assertTrue(gate.acquire(non_blocking=True))
        gate.release()

    def test_snapshot_disk_failure_is_cleaned_before_engine_run(self):
        resource = self.root / 'lens.lcp'
        resource.write_text('synthetic profile')
        req = {**self.request, 'adjustments': {'lens_correction': {
            'mode': 'lcp', 'path': str(resource), 'sha256': rt.digest(resource)}}}
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt.shutil, 'copy2', side_effect=OSError(rt.errno.ENOSPC, 'synthetic disk full')), patch.object(rt, 'run') as runner:
            self.error('INSUFFICIENT_DISK_SPACE', rt.dispatch, req)
            runner.assert_not_called()
        self.assertEqual(list(Path(req['output_dir']).iterdir()), [])
        self.assertEqual(resource.read_text(), 'synthetic profile')

    def test_cleanup_failure_does_not_replace_primary_error(self):
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=rt.ToolError('PROCESS_FAILED', 'primary synthetic renderer failure')), patch.object(rt.shutil, 'rmtree', side_effect=OSError(rt.errno.ENOSPC, 'synthetic cleanup failure')):
            with self.assertRaises(rt.ToolError) as caught:
                rt.dispatch(self.request)
        self.assertEqual(caught.exception.code, 'PROCESS_FAILED')
        self.assertIn('primary synthetic renderer failure', str(caught.exception))
        self.assertIn('cleanup incomplete at', str(caught.exception))
        self.assertTrue(list(Path(self.request['output_dir']).iterdir()))

    def test_render_timeout_schema_and_dispatch(self):
        validator = jsonschema.Draft202012Validator(rt.schema())
        for bad in [29, 3601, True, 45.5, '600']:
            req = {**self.request, 'timeout_seconds': bad}
            self.assertFalse(validator.is_valid(req))
            self.error('INVALID_REQUEST', rt.validate, req)
        captured = []
        def custom_timeout(argv, **kwargs):
            captured.append(kwargs['timeout'])
            return self.fake_render(argv, **{**kwargs, 'timeout': 300})
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=custom_timeout):
            result = rt.dispatch({**self.request, 'timeout_seconds': 600.0})
        self.assertEqual(captured, [600])
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual(manifest['request']['timeout_seconds'], 600)
        self.assertEqual(manifest['disk_preflight']['required_free_bytes'], rt.MIN_RENDER_FREE_BYTES)

    def test_json_subprocess_stdout_is_not_contaminated_by_stderr(self):
        completed = subprocess.CompletedProcess([], 0, '[{"ISO":100}]', 'nonfatal locale warning\n')
        with patch.object(rt.subprocess, 'run', return_value=completed):
            self.assertEqual(json.loads(rt.run(['/fake/exiftool'], stdout_only=True)), [{'ISO': 100}])
            self.assertIn('nonfatal locale warning', rt.run(['/fake/renderer']))
        with patch.object(rt.shutil, 'which', return_value='/fake/exiftool'), patch.object(rt.subprocess, 'run', return_value=completed):
            result = rt.dispatch({'action': 'inspect', 'source': str(self.source)})
            self.assertEqual(result['metadata'], [{'ISO': 100}])

    def test_partial_manifest_disk_failure_is_not_preserved_as_complete(self):
        session = self.root / 'partial-manifest-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Partial manifest'})
        before = session.read_bytes()
        original_write = Path.write_text
        def fail_manifest(path, text, *args, **kwargs):
            if path.name == 'manifest.json':
                original_write(path, '{"partial":', encoding='utf-8')
                raise OSError(rt.errno.ENOSPC, 'synthetic partial manifest write')
            return original_write(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', fail_manifest):
            self.error('INSUFFICIENT_DISK_SPACE', self.render_dispatch,
                       {**self.request, 'session_path': str(session), 'expected_revision': 0})
        self.assertEqual(session.read_bytes(), before)
        self.assertEqual(list(Path(self.request['output_dir']).iterdir()), [])

    def test_builtin_lcp_profile_and_domestic_lens_matching(self):
        alias = 'builtin:lcp:viltrox-af-50f2-z'
        # 1. Profile action
        prof, _ = rt.profile(alias, kind='lcp')
        self.assertEqual(prof['kind'], 'lcp')
        self.assertTrue(Path(prof['path']).is_file())
        self.assertEqual(prof['sha256'], rt.digest(prof['path']))
        self.error('INVALID_REQUEST', rt.profile, alias, 'pp3')
        # 2. Lens support check
        with patch.object(rt, 'engine', side_effect=rt.ToolError('ENGINE_UNAVAILABLE', 'isolated unit test')):
            chk = rt.lens_support_check([{'LensModel': 'Viltrox AF 50/2.0 Z'}])
        self.assertEqual(chk.get('experimental_profile_available'), alias)
        self.assertNotIn('builtin_profile_available', chk)
        self.assertIn(alias, chk['hint'])
        # 3. Session record_feedback and render with builtin LCP alias
        session = self.root / 'builtin-lcp-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Builtin LCP test'})
        fb = rt.dispatch({'action': 'session', 'operation': 'record_feedback', 'session_path': str(session),
                          'expected_revision': 0, 'user_feedback': 'use builtin lcp',
                          'adjustments': {'lens_correction': {'mode': 'lcp', 'path': alias, 'distortion': True, 'vignetting': True}}})
        eff_lc = fb['effective_adjustments']['lens_correction']
        self.assertEqual(eff_lc['path'], prof['path'])
        self.assertEqual(eff_lc['sha256'], prof['sha256'])
        result = self.render_dispatch({**self.request, 'session_path': str(session), 'expected_revision': 1})
        self.assertTrue(Path(result['output']).is_file())
        manifest = json.loads(Path(result['manifest']).read_text())
        ext_res = manifest['external_resources']['lens_profile']
        self.assertEqual(ext_res['canonical_path'], prof['path'])
        self.assertEqual(ext_res['sha256'], prof['sha256'])

    def test_builtin_lcp_path_only_patch_inherits_mode_and_rebinds_hash(self):
        alias = 'builtin:lcp:viltrox-af-50f2-z'
        previous = self.root / 'previous.lcp'
        previous.write_bytes(b'test-only previous resource')
        base = {'contrast': 12, 'lens_correction': {'mode': 'lcp', 'path': str(previous),
                'sha256': rt.digest(previous), 'vignetting': False}}
        req = {'adjustments': {'lens_correction': {'path': alias}}}
        result = rt.resolve_effective_adjustments(base, req, '5.13')
        prof, _ = rt.profile(alias, 'lcp')
        self.assertEqual(result['lens_correction'], {'mode': 'lcp', 'path': prof['path'],
                         'sha256': prof['sha256'], 'vignetting': False})
        self.assertEqual(result['contrast'], 12)
        self.assertEqual(base['lens_correction']['path'], str(previous))
        self.assertEqual(req['adjustments']['lens_correction'], {'path': alias})
        self.error('CHECKSUM_MISMATCH', rt.resolve_effective_adjustments, base,
                   {'adjustments': {'lens_correction': {'path': alias, 'sha256': '0' * 64}}}, '5.13')
        self.error('INVALID_REQUEST', rt.resolve_effective_adjustments, base,
                   {'reset_fields': ['lens_correction'], 'adjustments': {'lens_correction': {'path': alias}}}, '5.13')
        session = self.root / 'path-only-alias-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3),
                     'confirmed_brief': 'Explicit LCP integration experiment', 'adjustments': base})
        feedback = rt.dispatch({'action': 'session', 'operation': 'record_feedback',
                               'session_path': str(session), 'expected_revision': 0, **req})
        self.assertEqual(feedback['effective_adjustments'], result)
        rendered = self.render_dispatch({**self.request, 'session_path': str(session), 'expected_revision': 1})
        self.assertIn('UNVERIFIED_LENS_PROFILE', [w['code'] for w in rendered['warnings']])

    def test_builtin_lcp_discovery_is_independent_of_lensfun(self):
        alias = 'builtin:lcp:viltrox-af-50f2-z'
        scenarios = [patch.object(rt, 'engine', side_effect=rt.ToolError('ENGINE_UNAVAILABLE', 'test missing engine')),
                     patch.object(rt, 'engine', return_value={'path': '/missing/rt', 'version': '5.13'})]
        for scenario in scenarios:
            with self.subTest(scenario=scenario), scenario:
                result = rt.lens_support_check([{'LensModel': 'Viltrox AF 50/2.0 Z'}])
                self.assertEqual(result['status'], 'database_unavailable')
                self.assertEqual(result['experimental_profile_available'], alias)
                self.assertEqual(result['builtin_profile_status'], 'unverified')
                self.assertNotIn('builtin_profile_available', result)
                self.assertIn('未标定', result['hint'])
                self.assertFalse(result['optical_correction_verified'])

    def test_builtin_lcp_missing_or_modified_asset_is_not_advertised(self):
        alias = 'builtin:lcp:viltrox-af-50f2-z'
        modified = self.root / 'modified-builtin.lcp'
        modified.write_bytes(b'not the packaged calibration bytes')
        for resource in [self.root / 'absent.lcp', modified]:
            with self.subTest(resource=resource), patch.dict(rt.BUNDLED_PROFILES, {alias: resource}), \
                 patch.object(rt, 'engine', side_effect=rt.ToolError('ENGINE_UNAVAILABLE', 'test missing engine')):
                result = rt.lens_support_check([{'LensModel': 'Viltrox AF 50/2.0 Z'}])
                self.assertEqual(result['builtin_profile_status'], 'unavailable')
                self.assertNotIn('experimental_profile_available', result)
                self.assertNotIn('builtin_profile_available', result)
                expected = 'CHECKSUM_MISMATCH' if resource.exists() else 'FILE_NOT_FOUND'
                self.error(expected, rt.profile, alias, 'lcp')
        with patch.dict(rt.BUNDLED_PROFILES, {alias: modified}):
            self.error('CHECKSUM_MISMATCH', rt.resolve_effective_adjustments, {},
                       {'adjustments': {'lens_correction': {'mode': 'lcp', 'path': str(modified),
                         'sha256': rt.digest(modified)}}}, '5.13')

    def test_unverified_builtin_lcp_policy_survives_absolute_path_and_capabilities(self):
        alias = 'builtin:lcp:viltrox-af-50f2-z'
        prof, _ = rt.profile(alias, 'lcp')
        self.assertEqual(prof['builtin_profile']['calibration_status'], 'unverified')
        self.assertFalse(prof['builtin_profile']['recommended'])
        self.assertFalse(prof['effect_verified'])
        canonical, _ = rt.profile(prof['path'], 'lcp')
        self.assertEqual(canonical['builtin_profile'], prof['builtin_profile'])
        warning = rt.render_warnings({'lens_correction': {'mode': 'lcp', 'path': prof['path']}})
        self.assertEqual(warning[0]['code'], 'UNVERIFIED_LENS_PROFILE')
        with patch.object(rt, 'engine', side_effect=rt.ToolError('ENGINE_UNAVAILABLE', 'test missing engine')):
            caps = rt.dispatch({'action': 'capabilities', 'detail': 'summary'})
        self.assertFalse(caps['builtin_profile_metadata'][alias]['recommended'])
        self.assertEqual(caps['builtin_profile_metadata'][alias]['calibration_status'], 'unverified')

    def test_export_options_schema_and_runtime_validation(self):
        validator = jsonschema.Draft202012Validator(rt.schema())
        good = {**self.request, 'output_profile': {'path': '/absolute/output.icc', 'sha256': 'a' * 64},
                'preview_metadata': 'capture_camera', 'threads': 4}
        validator.validate(good)
        rt.validate(good)
        for fields in [{'threads': True}, {'threads': 0}, {'threads': 257}, {'threads': 1.5},
                       {'preview_metadata': 'all'}, {'output_profile': {'path': '/some.icc'}},
                       {'output_profile': {'path': '/some.icc', 'sha256': '0' * 64, 'extra': True}}]:
            with self.subTest(fields=fields):
                req = {**self.request, **fields}
                self.assertFalse(validator.is_valid(req))
                self.error('INVALID_REQUEST', rt.validate, req)

    def test_output_icc_override_snapshot_and_export_history(self):
        icc = self.root / '输出 profile.icc'
        icc.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        before = rt.digest(self.pp3)
        session = self.root / 'export-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Export test'})
        def render_with_icc(argv, **kwargs):
            text = Path(argv[argv.index('-p') + 1]).read_text()
            import configparser
            cfg = configparser.ConfigParser(interpolation=None); cfg.read_string(text)
            path = cfg['Color Management']['OutputProfile']
            self.assertTrue(path.startswith('file:'))
            payload = Path(path[5:]).read_bytes()
            Image.new('RGB', (64, 32), (120, 80, 40)).save(argv[argv.index('-o') + 1], icc_profile=payload)
            self.assertEqual(kwargs['threads'], 2)
            return 'fake ICC-aware renderer'
        req = {**self.request, 'session_path': str(session), 'expected_revision': 0,
               'output_profile': {'path': str(icc), 'sha256': rt.digest(icc)}, 'threads': 2}
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=render_with_icc):
            result = rt.dispatch(req)
        manifest = json.loads(Path(result['manifest']).read_text())
        resource = manifest['external_resources']['output_profile']
        self.assertEqual(rt.digest(resource['snapshot_path']), rt.digest(icc))
        self.assertEqual(result['export'], manifest['export'])
        self.assertTrue(result['export']['requested_icc_verified'])
        self.assertEqual(result['export']['omp_thread_limit'], '2')
        self.assertEqual(json.loads(session.read_text())['history'][-1]['export'], result['export'])
        self.assertEqual(rt.digest(self.pp3), before)

    def test_output_icc_invalid_hash_space_and_fallback_rejected(self):
        icc = self.root / 'output.icc'
        payload = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        icc.write_bytes(payload)
        self.error('CHECKSUM_MISMATCH', rt.output_profile_info, {'path': str(icc), 'sha256': '0' * 64})
        lab = self.root / 'lab.icc'; lab.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile('LAB')).tobytes())
        self.error('PROFILE_INVALID', rt.output_profile_info, {'path': str(lab), 'sha256': rt.digest(lab)})
        broken = self.root / 'invalid.icc'; broken.write_bytes(b'invalid ICC')
        self.error('PROFILE_INVALID', rt.output_profile_info, {'path': str(broken), 'sha256': rt.digest(broken)})
        icc.write_bytes(payload + b'fixture padding for distinct hash')
        req = {**self.request, 'output_profile': {'path': str(icc), 'sha256': rt.digest(icc)}}
        self.error('OUTPUT_PROFILE_MISMATCH', self.render_dispatch, req)
        self.assertEqual(list(Path(req['output_dir']).iterdir()), [])

    def test_preview_metadata_capture_camera_allowlist_and_geometry(self):
        source = self.root / 'source-capture.jpg'
        exif = Image.Exif(); exif[271] = 'NIKON'; exif[272] = 'Z 6_2'; exif[274] = 6
        exif[34853] = {1: 'N', 2: (10, 20, 30)}
        exif[34665] = {36867: '2026:10:24 19:09:19', 36881: '+08:00', 33437: 2.0,
                       34855: 100, 42036: 'Viltrox AF 50/2.0 Z', 42033: 'private-serial',
                       37500: b'private maker note', 40962: 4000, 40963: 2000}
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB', (400, 200)).save(source, icc_profile=icc, exif=exif)
        before = rt.digest(source)
        for policy in ['strip', 'capture', 'capture_camera']:
            target = self.root / (policy + '.jpg')
            result = rt.qc(source, preview_path=target, preview_metadata=policy)['preview']
            with Image.open(target) as preview:
                tags = preview.getexif(); nested = tags.get_ifd(34665)
                self.assertEqual(preview.size, (200, 400))
                if policy == 'strip':
                    self.assertEqual(len(tags), 0)
                else:
                    self.assertEqual(nested[36867], '2026:10:24 19:09:19')
                    self.assertEqual(nested[36881], '+08:00')
                    self.assertEqual((nested[40962], nested[40963]), preview.size)
                    self.assertEqual(tags[274], 1)
                    self.assertNotIn(34853, tags)
                    self.assertNotIn(42033, nested); self.assertNotIn(37500, nested)
                    self.assertEqual(271 in tags, policy == 'capture_camera')
                    self.assertEqual(34855 in nested, policy == 'capture_camera')
                self.assertEqual(result['metadata_policy'], policy)
        self.assertEqual(rt.digest(source), before)

    def test_thread_limit_only_overrides_child_environment(self):
        import os
        completed = subprocess.CompletedProcess([], 0, 'ok', '')
        with patch.dict(os.environ, {'OMP_NUM_THREADS': '3', 'OMP_THREAD_LIMIT': '6'}), patch.object(rt.subprocess, 'run', return_value=completed) as child:
            rt.run(['/fake/rt'])
            self.assertEqual(child.call_args.kwargs['env']['OMP_NUM_THREADS'], '3')
            rt.run(['/fake/rt'], threads=2)
            self.assertEqual(child.call_args.kwargs['env']['OMP_NUM_THREADS'], '2')
            self.assertEqual(child.call_args.kwargs['env']['OMP_THREAD_LIMIT'], '2')
            self.assertEqual(os.environ['OMP_NUM_THREADS'], '3')
            self.error('INVALID_REQUEST', rt.run, ['/fake/rt'], 30, None, False, False, 0)

    def test_late_feedback_exposes_uncommitted_artifact_details(self):
        session = self.root / 'late-detail.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Late feedback'})
        def feedback_during_render(argv, **kwargs):
            rt.dispatch({'action': 'session', 'operation': 'record_feedback', 'session_path': str(session),
                         'expected_revision': 0, 'user_feedback': 'Keep my latest request'})
            return self.fake_render(argv, **kwargs)
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=feedback_during_render):
            with self.assertRaises(rt.ToolError) as caught:
                rt.dispatch({**self.request, 'session_path': str(session), 'expected_revision': 0})
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        artifact = caught.exception.details['uncommitted_render']
        self.assertEqual(artifact['status'], 'uncommitted')
        self.assertTrue(Path(artifact['output']).is_file())
        self.assertTrue(Path(artifact['manifest']).is_file())
        self.assertEqual(json.loads(session.read_text())['revision'], 1)

    def make_history_prune_fixture(self):
        session = self.root / 'prune-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Prune fixture'})
        results = [self.render_dispatch({**self.request, 'session_path': str(session), 'expected_revision': i}) for i in range(3)]
        req = {'action': 'prune_history', 'session_path': str(session), 'expected_revision': 3,
               'manifest_paths': [results[0]['manifest']]}
        return session, results, req

    def test_history_prune_preview_apply_and_provenance_retention(self):
        session, results, req = self.make_history_prune_fixture()
        before = session.read_bytes(); source_hash = rt.digest(self.source)
        preview = rt.dispatch(req)
        self.assertEqual(preview['status'], 'preview')
        self.assertGreater(preview['reclaimable_logical_bytes'], 0)
        self.assertEqual(session.read_bytes(), before)
        self.assertTrue(Path(results[0]['output']).is_file())
        applied = rt.dispatch({**req, 'apply': True})
        self.assertEqual(applied['status'], 'complete')
        self.assertEqual(applied['session_revision'], 4)
        self.assertEqual(len(applied['deleted_files']), 2)
        self.assertFalse(Path(results[0]['output']).exists())
        self.assertFalse(Path(results[0]['preview']['path']).exists())
        self.assertTrue(Path(results[0]['manifest']).is_file())
        self.assertTrue((Path(results[0]['manifest']).parent / 'effective.pp3').is_file())
        self.assertTrue(Path(results[1]['output']).is_file()); self.assertTrue(Path(results[2]['output']).is_file())
        state = json.loads(session.read_text()); self.assertEqual(state['history'][0]['artifact_state'], 'pruned')
        self.assertEqual(rt.digest(self.source), source_hash)
        cleaned = rt.dispatch({'action': 'clean', 'session_path': str(session), 'output_dir': self.request['output_dir'], 'min_age_seconds': 0})
        self.assertEqual(cleaned['total_orphans'], 0)

    def test_history_prune_protects_latest_conflicts_unknown_and_sources(self):
        session, results, req = self.make_history_prune_fixture()
        before = session.read_bytes()
        for code, changed in [('PROTECTED_ARTIFACT', {'manifest_paths': [results[-1]['manifest']]}),
                              ('REVISION_CONFLICT', {'expected_revision': 2}),
                              ('INVALID_REQUEST', {'manifest_paths': [str(self.root / 'unknown.json')]})]:
            self.error(code, rt.dispatch, {**req, **changed, 'apply': True})
            self.assertEqual(session.read_bytes(), before)
        gate = rt.FileLock(Path(str(session) + '.render.gate')); self.assertTrue(gate.acquire(non_blocking=True))
        try:
            self.error('RENDER_IN_PROGRESS', rt.dispatch, {**req, 'apply': True})
        finally:
            gate.release()
        state = json.loads(before); state['source']['path'] = results[0]['output']; session.write_text(json.dumps(state))
        self.error('PROTECTED_ARTIFACT', rt.dispatch, {**req, 'apply': True})
        self.assertTrue(Path(results[0]['output']).is_file())

    def test_history_prune_commit_failure_precedes_deletion(self):
        session, results, req = self.make_history_prune_fixture()
        before = session.read_bytes()
        with patch.object(rt, 'atomic_write_json', side_effect=OSError('synthetic commit failure')):
            self.error('SESSION_COMMIT_FAILED', rt.dispatch, {**req, 'apply': True})
        self.assertEqual(session.read_bytes(), before)
        self.assertTrue(Path(results[0]['output']).is_file())
        manifest = Path(results[0]['manifest']); manifest.write_text('{}')
        self.error('CHECKSUM_MISMATCH', rt.dispatch, {**req, 'apply': True})
        self.assertTrue(Path(results[0]['output']).is_file())

    def test_history_prune_partial_delete_resume_and_final_commit_failure(self):
        session, results, req = self.make_history_prune_fixture()
        original_unlink = Path.unlink
        preview = Path(results[0]['preview']['path'])
        def fail_preview(path, *args, **kwargs):
            if path == preview:
                raise PermissionError('synthetic preview unlink failure')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_preview):
            partial = rt.dispatch({**req, 'apply': True})
        self.assertEqual(partial['status'], 'partial')
        self.assertEqual(json.loads(session.read_text())['history'][0]['artifact_state'], 'prune_partial')
        self.assertFalse(Path(results[0]['output']).exists()); self.assertTrue(preview.exists())
        real_write = rt.atomic_write_json; calls = []
        def fail_final(path, data):
            calls.append(True)
            if len(calls) == 2:
                raise OSError('synthetic final status write failure')
            return real_write(path, data)
        with patch.object(rt, 'atomic_write_json', side_effect=fail_final):
            self.error('SESSION_COMMIT_FAILED', rt.dispatch, {**req, 'expected_revision': 4, 'apply': True})
        state = json.loads(session.read_text()); self.assertEqual(state['history'][0]['artifact_state'], 'prune_pending')
        self.assertEqual(state['revision'], 5); self.assertFalse(preview.exists())
        recovered = rt.dispatch({**req, 'expected_revision': 5, 'apply': True})
        self.assertEqual(recovered['status'], 'complete')
        self.assertEqual(recovered['session_revision'], 6)
        self.assertEqual(json.loads(session.read_text())['history'][0]['artifact_state'], 'pruned')
        self.assertTrue(Path(results[-1]['output']).is_file())

    def test_history_prune_protects_declared_other_session_and_output_icc_anchors(self):
        session, results, req = self.make_history_prune_fixture()
        other = self.root / 'other-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(other),
                     'source': results[0]['output'], 'base_profile': str(self.pp3), 'confirmed_brief': 'Reuses older render'})
        before = session.read_bytes()
        self.error('PROTECTED_ARTIFACT', rt.dispatch, {**req, 'protect_session_paths': [str(other)], 'apply': True})
        self.assertEqual(session.read_bytes(), before)
        self.assertTrue(Path(results[0]['output']).is_file())
        other.write_text('{broken')
        self.error('CORRUPT_SESSION', rt.dispatch, {**req, 'protect_session_paths': [str(other)], 'apply': True})
        self.assertEqual(session.read_bytes(), before)
        state = json.loads(before)
        state['history'][0]['export']['output_profile'] = {'canonical_path': 'unanchored-relative.icc'}
        self.error('CORRUPT_SESSION', rt.check_session_resource_anchors, state, session)

    def test_clean_protects_output_icc_referenced_in_export_history(self):
        unbound = self.render_dispatch(self.request)
        folder = Path(unbound['manifest']).parent
        resource = folder / 'resource.icc'; resource.write_bytes(b'fingerprint-only guard fixture')
        session = self.root / 'icc-guard.json'
        initialized = rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                                  'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'ICC guard'})
        manifest = json.loads(Path(unbound['manifest']).read_text()); manifest['session_id'] = initialized['session_id']
        Path(unbound['manifest']).write_text(json.dumps(manifest))
        data = json.loads(session.read_text())
        data['history'].append({'revision': 0, 'user_feedback': 'Resource reference fixture',
                                'export': {'output_profile': {'canonical_path': str(resource), 'sha256': rt.digest(resource)}}})
        session.write_text(json.dumps(data))
        result = rt.dispatch({'action': 'clean', 'session_path': str(session), 'output_dir': self.request['output_dir'], 'min_age_seconds': 0, 'apply': True})
        self.assertEqual(result['total_orphans'], 0)
        self.assertTrue(resource.is_file())
        self.assertIn('protected_user_file_inside', [item['reason'] for item in result['skipped']])

    def test_history_prune_rejects_session_hardlink_alias_before_locking(self):
        import os
        session, results, req = self.make_history_prune_fixture()
        alias = self.root / 'same-session-alias.json'
        os.link(session, alias)
        before = session.read_bytes()
        self.error('INVALID_REQUEST', rt.dispatch, {**req, 'protect_session_paths': [str(alias)], 'apply': True})
        self.assertEqual(session.read_bytes(), before)
        self.assertTrue(Path(results[0]['output']).is_file())

    def test_prune_repeat_is_noop_without_session_revision_or_write(self):
        session, results, req = self.make_history_prune_fixture()
        first = rt.dispatch({**req, 'apply': True})
        self.assertEqual(first['session_revision'], 4)
        before = session.read_bytes()
        repeated = {**req, 'expected_revision': 4, 'apply': True, 'protect_session_paths': []}
        jsonschema.Draft202012Validator(rt.schema()).validate(repeated)
        with patch.object(rt, 'atomic_write_json', side_effect=AssertionError('No-op must not write session')):
            result = rt.dispatch(repeated)
        self.assertEqual(result['status'], 'no_op')
        self.assertEqual(result['session_revision'], 4)
        self.assertEqual(result['deleted_files'], [])
        self.assertEqual(result['reclaimable_logical_bytes'], 0)
        self.assertEqual(session.read_bytes(), before)
        self.error('REVISION_CONFLICT', rt.dispatch, {**repeated, 'expected_revision': 3})
        self.assertTrue(Path(results[-1]['output']).is_file())

    def test_prune_mixed_request_preserves_completed_record(self):
        session, results, req = self.make_history_prune_fixture()
        rt.dispatch({**req, 'apply': True})
        first_record = json.loads(session.read_text())['history'][0]
        mixed = {**req, 'expected_revision': 4, 'apply': True,
                 'manifest_paths': [results[0]['manifest'], results[1]['manifest']]}
        result = rt.dispatch(mixed)
        self.assertEqual(result['session_revision'], 5)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['deleted_files']), 2)
        state = json.loads(session.read_text())
        self.assertEqual(state['history'][0], first_record)
        self.assertEqual(state['history'][1]['artifact_state'], 'pruned')
        before = session.read_bytes()
        repeated = rt.dispatch({**mixed, 'expected_revision': 5})
        self.assertEqual(repeated['status'], 'no_op')
        self.assertEqual(session.read_bytes(), before)

    def test_prune_empty_protection_list_matches_omission(self):
        session, results, req = self.make_history_prune_fixture()
        empty = {**req, 'protect_session_paths': []}
        jsonschema.Draft202012Validator(rt.schema()).validate(empty)
        rt.validate(empty)
        self.assertEqual(rt.dispatch(empty), rt.dispatch(req))
        self.error('INVALID_REQUEST', rt.validate, {**req, 'protect_session_paths': ['same', 'same']})
        self.error('INVALID_REQUEST', rt.validate, {**req, 'protect_session_paths': ['']})

    def test_local_output_profiles_enumerates_icc_and_icm_without_name_allowlist(self):
        mac = self.root / 'RawTherapee.app/Contents/MacOS/rawtherapee-cli'
        linux = self.root / 'prefix/bin/rawtherapee-cli'
        for executable, relative_root in [(mac, self.root / 'RawTherapee.app/Contents/Resources/share/iccprofiles/output'),
                                           (linux, self.root / 'prefix/share/rawtherapee/iccprofiles/output')]:
            with self.subTest(executable=executable):
                executable.parent.mkdir(parents=True, exist_ok=True); executable.touch()
                self.assertEqual(rt.local_output_profiles(str(executable)), [])
                relative_root.mkdir(parents=True)
                for name in ['RTv4_sRGB.icc', 'RTv4_Wide.icc', 'ProPhoto.ICC', 'Custom Printer.ICM']:
                    (relative_root / name).write_bytes(b'discovery candidate, validated only when selected')
                (relative_root / 'ignored.txt').write_text('not an ICC candidate')
                (relative_root / 'directory.icc').mkdir()
                found = rt.local_output_profiles(str(executable))
                self.assertEqual({item['name'] for item in found}, {'RTv4_sRGB', 'RTv4_Wide', 'ProPhoto', 'Custom Printer'})
                self.assertTrue(all(Path(item['path']).is_absolute() for item in found))
                self.assertEqual(found, rt.local_output_profiles(str(executable)))

    def test_input_profile_partial_path_and_hash_inherit_custom_across_cwd(self):
        import copy
        folder = self.root / '资源'; folder.mkdir()
        original = folder / 'old.dcp'; original.write_bytes(b'old DCP contract fixture')
        replacement = folder / 'new.dcp'; replacement.write_bytes(b'new DCP contract fixture')
        base = {'input_profile': {'mode': 'custom', 'path': str(original), 'sha256': rt.digest(original)}, 'contrast': 12}
        req = {'adjustments': {'input_profile': {'path': 'new.dcp', 'sha256': rt.digest(replacement)}}}
        before = copy.deepcopy((base, req)); previous = Path.cwd()
        try:
            os.chdir(folder)
            result = rt.resolve_effective_adjustments(base, req, '5.13')
            self.assertEqual(result['input_profile']['path'], str(replacement.resolve()))
            self.assertEqual(result['input_profile']['mode'], 'custom')
            self.assertEqual(result['contrast'], 12)
            self.error('CHECKSUM_MISMATCH', rt.resolve_effective_adjustments, base,
                       {'adjustments': {'input_profile': {'path': 'new.dcp'}}}, '5.13')
            self.error('INVALID_REQUEST', rt.resolve_effective_adjustments, base,
                       {**req, 'reset_fields': ['input_profile']}, '5.13')
        finally:
            os.chdir(previous)
        self.assertEqual((base, req), before)
        rt.check_session_resource_anchors({'effective_adjustments': result}, self.root / 'anchor-check.json')

    def test_non_srgb_jpeg_warning_uses_rendered_profile_and_keeps_lens_warning(self):
        alias, _ = rt.profile('builtin:lcp:viltrox-af-50f2-z', 'lcp')
        adj = {'lens_correction': {'mode': 'lcp', 'path': alias['path']}}
        for description in ['RTv4_Rec2020', 'RTv4_Wide', 'ProPhoto RGB', 'Custom unknown profile', None]:
            with self.subTest(description=description):
                warnings = rt.render_warnings(adj, 'jpeg', description)
                self.assertEqual({w['code'] for w in warnings},
                                 {'UNVERIFIED_LENS_PROFILE', 'JPEG_DELIVERY_PROFILE_REVIEW'})
                self.assertNotIn('JPEG_DELIVERY_PROFILE_REVIEW', [w['code'] for w in rt.render_warnings(adj, 'tiff16', description)])
        for description in ['sRGB built-in', 'RTv4_sRGB', 'sRGB IEC61966-2.1']:
            self.assertEqual(rt.render_warnings({}, 'jpeg', description), [])
        real_qc = rt.qc
        def qc_unknown(*args, **kwargs):
            result = real_qc(*args, **kwargs)
            result['icc_description'] = 'Test renderer custom ICC description'
            return result
        with patch.object(rt, 'qc', side_effect=qc_unknown):
            rendered = self.render_dispatch(self.request)
        manifest = json.loads(Path(rendered['manifest']).read_text())
        self.assertEqual(rendered['warnings'], manifest['warnings'])
        self.assertIn('JPEG_DELIVERY_PROFILE_REVIEW', [w['code'] for w in rendered['warnings']])
        self.assertEqual(rendered['export']['embedded_icc_description'], 'Test renderer custom ICC description')

    def test_capabilities_output_profiles_include_current_hash_and_isolate_unreadable(self):
        executable = self.root / 'prefix/bin/rawtherapee-cli'
        executable.parent.mkdir(parents=True); executable.touch()
        directory = self.root / 'prefix/share/rawtherapee/iccprofiles/output'; directory.mkdir(parents=True)
        good = directory / 'custom.icc'; good.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        blocked = directory / 'unreadable.icm'; blocked.write_bytes(b'error isolation fixture')
        original = rt.digest
        def read_digest(path):
            if Path(path) == blocked:
                raise PermissionError('synthetic ICC permission denial')
            return original(path)
        with patch.object(rt, 'engine', return_value={'path': str(executable), 'version': '5.13'}), patch.object(rt, 'digest', side_effect=read_digest):
            caps = rt.dispatch({'action': 'capabilities', 'detail': 'summary'})
        entries = {p['name']: p for p in caps['output_profiles']}
        self.assertEqual(entries['custom']['sha256'], rt.digest(good))
        self.assertIsNone(entries['unreadable']['sha256'])
        self.assertEqual(entries['unreadable']['error']['code'], 'PROFILE_UNREADABLE')
        selected = entries['custom']
        parsed = rt.dispatch({'action': 'profile', 'profile': selected['path'], 'kind': 'icc'})
        self.assertEqual(parsed['sha256'], selected['sha256'])
        self.assertEqual(parsed['icc']['color_space'], 'RGB')
        good.write_bytes(good.read_bytes() + b'changed after discovery')
        self.error('CHECKSUM_MISMATCH', rt.output_profile_info, {'path': selected['path'], 'sha256': selected['sha256']})

    def test_profile_icc_parses_binary_structure_without_claiming_rgb_output(self):
        for space in ['sRGB', 'LAB']:
            with self.subTest(space=space):
                path = self.root / (space + '.ICM')
                path.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile(space)).tobytes())
                before = path.read_bytes()
                result = rt.dispatch({'action': 'profile', 'profile': str(path), 'kind': 'icc'})
                self.assertEqual(result['sha256'], rt.digest(path))
                self.assertEqual(result['icc']['color_space'], 'RGB' if space == 'sRGB' else 'Lab')
                self.assertTrue(result['icc']['description'])
                self.assertTrue(result['icc']['device_class'])
                self.assertFalse(result['effect_verified'])
                self.assertEqual(path.read_bytes(), before)
                if space == 'LAB':
                    self.error('PROFILE_INVALID', rt.output_profile_info, {'path': str(path), 'sha256': result['sha256']})
        broken = self.root / 'broken.icc'; broken.write_bytes(b'not an ICC container')
        self.error('PROFILE_INVALID', rt.dispatch, {'action': 'profile', 'profile': str(broken), 'kind': 'icc'})
        valid = self.root / 'sRGB.ICM'
        self.error('INVALID_REQUEST', rt.dispatch, {'action': 'profile', 'profile': str(valid)})
        real_find = rt.importlib.util.find_spec
        with patch.object(rt.importlib.util, 'find_spec', side_effect=lambda name: None if name == 'PIL' else real_find(name)):
            self.error('DEPENDENCY_MISSING', rt.dispatch, {'action': 'profile', 'profile': str(valid), 'kind': 'icc'})
            self.assertEqual(rt.profile(self.pp3)[0]['kind'], 'pp3')

    def test_icc_discovery_to_render_uses_only_tool_api_values(self):
        executable = self.root / 'prefix/bin/rawtherapee-cli'; executable.parent.mkdir(parents=True); executable.touch()
        directory = self.root / 'prefix/share/rawtherapee/iccprofiles/output'; directory.mkdir(parents=True)
        path = directory / 'Test RGB.icc'; path.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        def save_requested_icc(argv, **kwargs):
            import configparser
            cfg = configparser.ConfigParser(interpolation=None); cfg.read(argv[argv.index('-p') + 1])
            icc = Path(cfg['Color Management']['OutputProfile'][5:]).read_bytes()
            Image.new('RGB', (64, 32), (120, 80, 40)).save(argv[argv.index('-o') + 1], icc_profile=icc)
            return 'fake renderer using the API-selected ICC snapshot'
        with patch.object(rt, 'engine', return_value={'path': str(executable), 'version': '5.13'}), patch.object(rt, 'run', side_effect=save_requested_icc):
            selected = rt.dispatch({'action': 'capabilities', 'detail': 'summary'})['output_profiles'][0]
            parsed = rt.dispatch({'action': 'profile', 'profile': selected['path'], 'kind': 'icc'})
            request = {**self.request, 'output_profile': {k: selected[k] for k in ('path', 'sha256')}}
            jsonschema.Draft202012Validator(rt.schema()).validate(request)
            rendered = rt.dispatch(request)
        self.assertEqual(parsed['sha256'], selected['sha256'])
        self.assertEqual(rendered['qc']['icc_sha256'], selected['sha256'])
        self.assertTrue(rendered['export']['requested_icc_verified'])

    def test_feedback_top_level_exposure_persists_inherits_and_renders(self):
        session = self.root / 'feedback-exposure.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Keep warm',
                     'keep': ['warm light'], 'exposure_ev': 0.2, 'adjustments': {'contrast': 12}})
        feedback = {'action': 'session', 'operation': 'record_feedback', 'session_path': str(session),
                    'expected_revision': 0, 'exposure_ev': -0.4, 'user_feedback': 'Slightly darker'}
        jsonschema.Draft202012Validator(rt.schema()).validate(feedback)
        result = rt.dispatch(feedback)
        self.assertEqual(result['effective_adjustments'], {'contrast': 12, 'exposure_ev': -0.4})
        self.assertEqual(result['keep'], ['warm light'])
        state = rt.dispatch({'action': 'session', 'operation': 'load', 'session_path': str(session)})['session']
        self.assertEqual(state['history'][0]['effective_adjustments']['exposure_ev'], 0.2)
        req = {**self.request, 'session_path': str(session), 'expected_revision': 1}; req.pop('exposure_ev')
        rendered = self.render_dispatch(req)
        manifest = json.loads(Path(rendered['manifest']).read_text())
        self.assertEqual(manifest['effective_adjustments']['exposure_ev'], -0.4)
        pp3 = (Path(rendered['manifest']).parent / 'effective.pp3').read_text()
        self.assertIn('Compensation=-0.4', pp3)
        reset = rt.dispatch({**feedback, 'expected_revision': 2, 'reset_fields': ['exposure_ev'], 'exposure_ev': 0.5})
        self.assertEqual(reset['effective_adjustments'], {'contrast': 12, 'exposure_ev': 0.5})
        nested = {k: v for k, v in feedback.items() if k != 'exposure_ev'}
        nested.update(expected_revision=3, adjustments={'exposure_ev': 0.8})
        self.assertEqual(rt.dispatch(nested)['effective_adjustments']['exposure_ev'], 0.8)

    def test_feedback_exposure_invalid_or_duplicate_never_mutates_session(self):
        session = self.root / 'invalid-feedback-exposure.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(session),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'Stable intent'})
        before = session.read_bytes()
        req = {'action': 'session', 'operation': 'record_feedback', 'session_path': str(session), 'expected_revision': 0}
        validator = jsonschema.Draft202012Validator(rt.schema())
        for value in [True, '0.3', -2.01, 2.01, float('nan'), float('inf')]:
            with self.subTest(value=value):
                invalid = {**req, 'exposure_ev': value}
                if not isinstance(value, float) or np.isfinite(value):
                    self.assertFalse(validator.is_valid(invalid))
                self.error('INVALID_REQUEST', rt.dispatch, invalid)
                self.assertEqual(session.read_bytes(), before)
        duplicate = {**req, 'exposure_ev': 0.3, 'adjustments': {'exposure_ev': 0.3}}
        self.error('INVALID_REQUEST', rt.dispatch, duplicate)
        self.assertEqual(session.read_bytes(), before)
        for ev in [-2, 2]:
            validator.validate({**req, 'exposure_ev': ev})
        rt.dispatch({**req, 'exposure_ev': 0.1})
        saved = session.read_bytes()
        self.error('REVISION_CONFLICT', rt.dispatch, {**req, 'exposure_ev': 0.2})
        self.assertEqual(session.read_bytes(), saved)

    def test_common_control_contract_rejects_bad_patches(self):
        validator = jsonschema.Draft202012Validator(rt.schema())
        bad = [
            {'shadows_highlights': {'shadows': True}}, {'shadows_highlights': {'shadows': -1}},
            {'shadows_highlights': {'shadow_tonal_width': 0}}, {'vibrance': {'pastels': 101}},
            {'vibrance': {'protect_skin': 1}}, {'local_contrast': {'amount': 1.1}},
            {'graduated_filter': {'strength': 5.1}}, {'graduated_filter': {'center_y': 101}},
            {'black_white': {'method': 'magic'}}, {'transform': {'rotate': 45}},
            {'transform': {'straighten': 46}}, {'perspective': {'vertical': -101}},
            {'resize': {'long_edge': 31}}, {'resize': {'allow_upscaling': 1}},
            {'crop': {'x': -1}}, {'crop': {'width': 0}},
            {'hsv_equalizer': {'hue_curve': [[0, .5]]}},
            {'hsv_equalizer': {'hue_curve': [[0, .5, .5], [.5, .5]]}},
            {'hsv_equalizer': {'hue_curve': [[0, .5], [.5, 2]]}},
            {'hsv_equalizer': {'hue_curve': [[True, .5], [.5, .5]]}},
        ]
        for adj in bad:
            with self.subTest(adj=adj):
                self.assertFalse(validator.is_valid({**self.request, 'adjustments': adj}))
                self.error('INVALID_REQUEST', rt.dispatch, {**self.request, 'adjustments': adj})
        for adj in [{'resize': {'enabled': True}}, {'crop': {'enabled': True}},
                    {'hsv_equalizer': {'enabled': True}},
                    {'hsv_equalizer': {'hue_curve': [[0, .5], [0, .6]]}},
                    {'hsv_equalizer': {'hue_curve': [[.5, .5], [.2, .6]]}},
                    {'hsv_equalizer': {'hue_curve': [[0, .5], [1, .6]]}},
                    {'hsv_equalizer': {'hue_curve': [[0, float('nan')], [.5, .6]]}}]:
            self.error('INVALID_REQUEST', rt.resolve_effective_adjustments, {}, {'adjustments': adj}, '5.13')

    def test_common_control_schema_mapping_and_normalization(self):
        adj = {'shadows_highlights': {'shadows': 30.0, 'highlights': 20},
               'vibrance': {'pastels': 10, 'saturated': -5, 'protect_skin': True},
               'local_contrast': {'amount': .3, 'radius': 30},
               'graduated_filter': {'strength': .5, 'degree': 0, 'center_y': -10},
               'black_white': {'enabled': False}, 'perspective': {'vertical': 5},
               'transform': {'rotate': 90.0, 'straighten': 1.5, 'autofill': True, 'horizontal_flip': True},
               'hsv_equalizer': {'saturation_curve': [[0, .4], [.5, .5]]}}
        jsonschema.Draft202012Validator(rt.schema()).validate({**self.request, 'adjustments': adj})
        res = self.render_dispatch({**self.request, 'adjustments': adj})
        data = json.loads(Path(res['manifest']).read_text())
        self.assertIs(type(data['effective_adjustments']['transform']['rotate']), int)
        cfg = rt.configparser.ConfigParser();cfg.read(Path(res['manifest']).parent / 'effective.pp3')
        expected = {'Shadows & Highlights': {'Enabled': 'true', 'Shadows': '30', 'Highlights': '20'},
                    'Vibrance': {'Pastels': '10', 'Saturated': '-5', 'PastSatTog': 'false', 'ProtectSkins': 'true'},
                    'Local Contrast': {'Amount': '0.3'}, 'Gradient': {'Strength': '0.5'},
                    'Black & White': {'Enabled': 'false', 'Method': 'Desaturation'},
                    'Perspective': {'Method': 'simple', 'Vertical': '5'},
                    'Coarse Transformation': {'Rotate': '90', 'HorizontalFlip': 'true'},
                    'Rotation': {'Degree': '1.5'}, 'Common Properties for Transformations': {'AutoFill': 'true'},
                    'HSV Equalizer': {'SCurve': '1;0;0.4;0.35;0.35;0.5;0.5;0.35;0.35;'}}
        for sec, fields in expected.items():
            for key, value in fields.items():self.assertEqual(cfg[sec][key], value)

    def test_common_adjustments_persist_merge_disable_reset_and_cas(self):
        sess = self.root / 'common-session.json'
        adj = {'shadows_highlights': {'shadows': 25, 'highlights': 10},
               'vibrance': {'pastels': 15, 'protect_skin': True}, 'resize': {'long_edge': 640},
               'hsv_equalizer': {'hue_curve': [[0, .6], [.5, .5]], 'value_curve': [[0, .5], [.5, .6]]},
               'transform': {'rotate': 90, 'horizontal_flip': True}}
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(sess),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'keep warm',
                     'keep': ['warm'], 'adjustments': adj})
        feedback = {'action': 'session', 'operation': 'record_feedback', 'session_path': str(sess), 'expected_revision': 0,
                    'adjustments': {'shadows_highlights': {'shadows': 35}, 'vibrance': {'enabled': False},
                                    'hsv_equalizer': {'hue_curve': [[0, .5], [.5, .5]]}}}
        res = rt.dispatch(feedback);effective = res['effective_adjustments']
        self.assertEqual(effective['shadows_highlights'], {'shadows': 35, 'highlights': 10})
        self.assertEqual(effective['vibrance'], {'pastels': 15, 'protect_skin': True, 'enabled': False})
        self.assertEqual(effective['hsv_equalizer']['value_curve'], adj['hsv_equalizer']['value_curve'])
        snapshot = sess.read_bytes();self.error('REVISION_CONFLICT', rt.dispatch, feedback)
        self.assertEqual(snapshot, sess.read_bytes())
        res = rt.dispatch({**feedback, 'expected_revision': 1, 'reset_fields': ['vibrance', 'resize'], 'adjustments': {}})
        self.assertNotIn('vibrance', res['effective_adjustments']);self.assertNotIn('resize', res['effective_adjustments'])
        self.assertEqual(rt.session_load({'session_path': str(sess)})['keep'], ['warm'])
        self.assertEqual(res['effective_adjustments']['transform'], adj['transform'])

    def test_new_capabilities_are_available_for_raw_and_jpeg(self):
        categories = set(rt.COMMON_CONTROLS) | {'transform', 'crop', 'hsv_equalizer'}
        with patch.object(rt, 'engine', return_value={'version': '5.13'}):
            for source_type in ['raw', 'jpeg']:
                caps = rt.dispatch({'action': 'capabilities', 'source_type': source_type, 'detail': 'summary'})
                self.assertTrue(categories <= set(caps['available_adjustments']))
            caps = rt.dispatch({'action': 'capabilities', 'parameters': sorted(categories), 'detail': 'full'})
            self.assertEqual(set(caps['controllable_parameters']), categories)
        for category in categories:
            self.error('UNSUPPORTED_ADJUSTMENTS', rt.resolve_effective_adjustments, {},
                       {'adjustments': {category: {'enabled': False} if category not in ['transform', 'perspective'] else
                                                   ({'rotate': 90} if category == 'transform' else {'vertical': 1})}}, '5.12')

    def crop_from_reference(self):
        ref = self.render_dispatch({**self.request, 'adjustments': {'crop': {'enabled': False}, 'resize': {'enabled': False}}})
        return {'x': 4, 'y': 3, 'width': 24, 'height': 16, 'reference_manifest': ref['manifest']}, ref

    def test_crop_reference_binding_bounds_and_geometry(self):
        crop, ref = self.crop_from_reference()
        effective = rt.resolve_effective_adjustments({}, {'adjustments': {'crop': crop}}, '5.13')
        bound = effective['crop']
        self.assertEqual(bound['reference_sha256'], rt.digest(ref['manifest']))
        self.assertTrue(Path(bound['reference_manifest']).is_absolute())
        info = rt.crop_reference_info(effective, rt.digest(self.source), rt.digest(self.pp3))
        self.assertEqual((info['width'], info['height']), (64, 32))
        for field, value in [('x', 50), ('y', 30), ('width', 65), ('height', 33)]:
            self.error('INVALID_REQUEST', rt.crop_reference_info,
                       {'crop': {**bound, field: value}}, rt.digest(self.source), rt.digest(self.pp3))
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info, effective, '0'*64, rt.digest(self.pp3))
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info,
                   {**effective, 'transform': {'rotate': 90}}, rt.digest(self.source), rt.digest(self.pp3))
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info,
                   effective, rt.digest(self.source), '0'*64)
        self.error('CHECKSUM_MISMATCH', rt.resolve_effective_adjustments, {},
                   {'adjustments': {'crop': {**crop, 'reference_sha256': '0'*64}}}, '5.13')
        pp3 = Path(ref['manifest']).parent / 'effective.pp3'; pp3.write_text(pp3.read_text()+'\n# changed\n')
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info, effective, rt.digest(self.source), rt.digest(self.pp3))

    def test_crop_reference_changed_or_invalid_rejected_before_engine(self):
        crop, ref = self.crop_from_reference()
        effective = rt.resolve_effective_adjustments({}, {'adjustments': {'crop': crop}}, '5.13')
        path = Path(ref['manifest']);path.write_text(path.read_text()+'\n')
        with patch.object(rt, 'engine', return_value={'version': '5.13', 'path': '/fake/rt'}), patch.object(rt, 'run') as run:
            self.error('CHECKSUM_MISMATCH', rt.dispatch, {**self.request, 'adjustments': effective})
            run.assert_not_called()
        path.write_text('[]')
        bad = rt.resolve_effective_adjustments({}, {'adjustments': {'crop': crop}}, '5.13')
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info, bad, rt.digest(self.source), rt.digest(self.pp3))

    def test_crop_reference_cannot_be_resized_or_cropped_base(self):
        crop, ref = self.crop_from_reference();path = Path(ref['manifest']);data = json.loads(path.read_text())
        data['effective_adjustments']['resize'] = {'long_edge': 64};path.write_text(json.dumps(data))
        adj = rt.resolve_effective_adjustments({}, {'adjustments': {'crop': crop}}, '5.13')
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info, adj, rt.digest(self.source), rt.digest(self.pp3))
        data['effective_adjustments']['resize'] = {'enabled': False};path.write_text(json.dumps(data))
        pp3 = path.parent / 'effective.pp3';pp3.write_text(PP3+'\n[Crop]\nEnabled=true\n')
        data['effective_pp3_sha256'] = rt.digest(pp3);path.write_text(json.dumps(data))
        adj = rt.resolve_effective_adjustments({}, {'adjustments': {'crop': crop}}, '5.13')
        self.error('INVALID_CROP_REFERENCE', rt.crop_reference_info, adj, rt.digest(self.source), rt.digest(self.pp3))

    def test_crop_session_inheritance_and_disable_does_not_need_reference_file(self):
        crop, ref = self.crop_from_reference();sess = self.root / 'crop-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(sess),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'crop experiment',
                     'adjustments': {'crop': crop, 'contrast': 5}})
        feedback = {'action': 'session', 'operation': 'record_feedback', 'session_path': str(sess), 'expected_revision': 0,
                    'adjustments': {'crop': {'x': 8}}}
        res = rt.dispatch(feedback);self.assertEqual(res['effective_adjustments']['crop']['width'], 24)
        Path(ref['manifest']).unlink()
        res = rt.dispatch({**feedback, 'expected_revision': 1, 'adjustments': {'crop': {'enabled': False}}})
        self.assertFalse(res['effective_adjustments']['crop']['enabled'])
        res = rt.dispatch({**feedback, 'expected_revision': 2, 'reset_fields': ['crop'], 'adjustments': {}})
        self.assertEqual(res['effective_adjustments'], {'contrast': 5})

    def test_crop_and_resize_output_size_mismatch_cannot_commit(self):
        crop, ref = self.crop_from_reference();sess = self.root / 'size-session.json'
        rt.dispatch({'action': 'session', 'operation': 'init', 'session_path': str(sess),
                     'source': str(self.source), 'base_profile': str(self.pp3), 'confirmed_brief': 'crop'})
        snapshot = sess.read_bytes()
        for adj in [{'crop': crop}, {'resize': {'long_edge': 32}}]:
            self.error('OUTPUT_GEOMETRY_MISMATCH', self.render_dispatch,
                       {**self.request, 'session_path': str(sess), 'expected_revision': 0, 'adjustments': adj})
            self.assertEqual(sess.read_bytes(), snapshot)
        self.assertEqual(len(list((self.root/'out').iterdir())), 1)  # reference only
        rt.verify_output_geometry({'resize': {'long_edge': 128}}, {'width': 64, 'height': 32}, None)
        self.error('OUTPUT_GEOMETRY_MISMATCH', rt.verify_output_geometry,
                   {'resize': {'long_edge': 128, 'allow_upscaling': True}}, {'width': 64, 'height': 32}, None)

    def test_crop_success_commits_reference_and_pixels(self):
        crop, ref = self.crop_from_reference()
        def crop_render(argv, **kwargs):
            path = argv[argv.index('-o')+1]
            icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
            Image.new('RGB', (24,16), (100,120,140)).save(path, icc_profile=icc)
            return 'mock crop'
        with patch.object(rt, 'engine', return_value={'path': '/fake/rt', 'version': '5.13'}), patch.object(rt, 'run', side_effect=crop_render):
            res = rt.dispatch({**self.request, 'adjustments': {'crop': crop}})
        data = json.loads(Path(res['manifest']).read_text())
        self.assertTrue(data['geometry']['output_dimensions_checked'])
        self.assertEqual(data['geometry']['crop_reference']['sha256'], rt.digest(ref['manifest']))
        self.assertIn('reference_sha256', data['effective_adjustments']['crop'])
        self.assertEqual((res['qc']['width'], res['qc']['height']), (24,16))

    def test_macos_c_locale_uses_utf8_only_in_child_environment(self):
        with patch.dict(rt.os.environ, {'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8'}), patch.object(rt.platform, 'system', return_value='Darwin'), patch.object(rt.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')) as run:
            self.assertEqual(rt.run(['/fake/rt', '中文路径.jpg']), 'ok')
            self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'en_US.UTF-8')
            self.assertEqual(rt.os.environ['LC_ALL'], 'C.UTF-8')
        with patch.dict(rt.os.environ, {'LC_ALL': 'zh_CN.UTF-8'}), patch.object(rt.platform, 'system', return_value='Darwin'), patch.object(rt.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            rt.run(['/fake/rt'])
            self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'zh_CN.UTF-8')

if __name__ == '__main__':
    unittest.main()

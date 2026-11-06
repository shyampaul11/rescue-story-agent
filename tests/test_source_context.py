"""Synthetic naming/date/evidence tests; no private Drive access or paid requests."""
from datetime import date
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from ai_config import AnalysisSettings
from animal_profile import AnimalContext, AnimalSources, AnimalProfile
import ai_service as ai
import analysis_media as media
import analysis_ui as ui
from source_context import parse_animal_label, metadata_date, photo_date_reference
from test_ai_service import empty_profile, record
from test_analysis_ui import APP
from editorial_test_support import isolated_editorial_storage


def dated_jpeg(original='2024:05:02 10:11:12', generic='2024:05:03 08:00:00'):
    exif = Image.Exif()
    exif[271] = 'PRIVATE DEVICE MUST NOT BE SENT'
    exif[306] = generic
    if original is not None:
        exif[34665] = {36867: original}
    output = BytesIO()
    Image.new('RGB', (64, 32), 'brown').save(output, format='JPEG', exif=exif)
    return output.getvalue()


def selected_photo():
    metadata = photo_date_reference('photo:1', [('exif_datetime_original', '2024:05:02 10:11:12')], date(2026, 1, 1))
    return media.PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg', photo_date=metadata)


class ContextTests(unittest.TestCase):
    def test_parentheses_and_square_brackets_identify_rescuer(self):
        for value in ['Azra (Sophie)', 'Azra [Sophie]', ' Azra  ( Sophie ) ']:
            with self.subTest(value=value):
                context = parse_animal_label(value)
                self.assertEqual(context.animal_name, 'Azra')
                self.assertEqual(context.rescuer_name, 'Sophie')

    def test_names_with_spaces_accents_and_punctuation(self):
        context = parse_animal_label("Azra Junior (José van Dijk-Smit)")
        self.assertEqual(context.animal_name, 'Azra Junior')
        self.assertEqual(context.rescuer_name, 'José van Dijk-Smit')

    def test_plain_name_has_unknown_rescuer(self):
        context = parse_animal_label('Azra')
        self.assertEqual(context.animal_name, 'Azra')
        self.assertIsNone(context.rescuer_name)

    def test_ambiguous_or_empty_names_not_guessed(self):
        for value in [None, '', 'Azra (Sophie) (Other)', 'Azra (', '(Sophie)', 'Azra []']:
            with self.subTest(value=value):
                self.assertIsNone(parse_animal_label(value).rescuer_name)

    def test_context_allowlist_rejects_unrelated_fields(self):
        for key in ['folder', 'filename', 'drive_id', 'website_text', 'gps']:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                AnimalContext(**{key: 'DO NOT SEND'})

    def test_payload_adds_only_parsed_names_and_attribution(self):
        context = parse_animal_label('Azra (Sophie)')
        payload = ai.build_payload(AnimalSources(history='A reported story.'), [], AnalysisSettings(), context)
        value = json.loads(payload[1]['content'][1]['text'])
        self.assertEqual(value['allowed_identity_and_attribution'], {'animal_name': 'Azra', 'rescuer_name': 'Sophie'})
        self.assertNotIn('Azra (Sophie)', json.dumps(payload))
        self.assertEqual(json.loads(payload[1]['content'][0]['text'])['History']['text'], 'A reported story.')

    def test_profile_assigns_rescuer_to_text_evidence_and_keeps_dog_name_separate(self):
        data = empty_profile().model_dump()
        data['evidence'] = [record(quote='Azra was rescued.', reported_by='Fabricated reporter')]
        profile = ai.validate_profile(AnimalProfile.model_validate(data), AnimalSources(history='Azra was rescued.'), [],
                                      parse_animal_label('Azra (Sophie)'))
        self.assertEqual(profile.identity.animal_name, 'Azra')
        self.assertEqual(profile.source_attribution.rescuer_name, 'Sophie')
        self.assertEqual(profile.source_attribution.provided_sections, ['history', 'other_info', 'updates'])
        self.assertEqual(profile.evidence[0].reported_by, 'Sophie')
        self.assertEqual(profile.evidence[-1].source_type, 'ANIMAL_LABEL')

    def test_model_cannot_use_rescuer_as_dog_name(self):
        data = empty_profile().model_dump()
        data['identity'].update(animal_name='Sophie', name_evidence_id='e1')
        data['evidence'] = [record(ref='animal_label', quote='Sophie', source_type='ANIMAL_LABEL')]
        with self.assertRaises(ValueError):
            ai.validate_profile(AnimalProfile.model_validate(data), AnimalSources(), [], parse_animal_label('Azra (Sophie)'))

    def test_manual_source_does_not_inherit_rescuer(self):
        profile = ai.validate_profile(empty_profile(), AnimalSources(history='A manual story.'), [])
        self.assertIsNone(profile.source_attribution.rescuer_name)
        self.assertEqual(profile.source_attribution.provided_sections, [])


class PhotoDateTests(unittest.TestCase):
    def test_known_metadata_formats_preserve_local_calendar_date(self):
        for value in ['2024:05:02 10:11:12', '2024-05-02T23:59:00-04:00', '2024-05-02', '2024-05-02T10:11:12Z']:
            with self.subTest(value=value):
                self.assertEqual(metadata_date(value), date(2024, 5, 2))

    def test_ambiguous_damaged_and_invalid_dates_remain_unknown(self):
        for value in ['02/05/2024', '2024:02:30 10:11:12', 'not-a-date', None]:
            with self.subTest(value=value):
                self.assertIsNone(metadata_date(value))

    def test_original_capture_date_wins_and_conflict_is_flagged(self):
        reference = photo_date_reference('photo:1', [('exif_datetime_original', '2024:05:02 10:11:12'),
            ('drive_photo_time', '2024-05-03T10:00:00Z')], date(2026, 1, 1))
        self.assertEqual(reference.reference_date, '2024-05-02')
        self.assertEqual(reference.original_value, '2024:05:02 10:11:12')
        self.assertIn('disagree', reference.note)
        self.assertEqual(reference.verification_status, 'metadata_not_independently_verified')

    def test_future_dates_ignored_with_warning(self):
        reference = photo_date_reference('photo:1', [('drive_photo_time', '2099-01-01T00:00:00Z')], date(2026, 1, 1))
        self.assertIsNone(reference.reference_date)
        self.assertIn('future', reference.note)

    def test_missing_capture_date_has_no_upload_or_modified_fallback(self):
        reference = photo_date_reference('photo:1', [])
        self.assertIsNone(reference.reference_date)
        self.assertEqual(reference.date_source, 'unknown')
        self.assertIn('not substituted', reference.note)

    def test_embedded_date_survives_as_separate_record_while_exif_is_stripped(self):
        jpeg, reference = media.prepare_photo(dated_jpeg(), AnalysisSettings(), 'photo:1')
        self.assertEqual(reference.reference_date, '2024-05-02')
        self.assertEqual(reference.date_source, 'exif_datetime_original')
        with Image.open(BytesIO(jpeg)) as prepared:
            self.assertFalse(prepared.getexif())
        self.assertNotIn(b'PRIVATE DEVICE', jpeg)

    def test_drive_photo_date_used_when_no_original_exif(self):
        jpeg, reference = media.prepare_photo(dated_jpeg(original=None), AnalysisSettings(), 'photo:1', '2024-04-01T10:00:00Z')
        self.assertEqual(reference.reference_date, '2024-04-01')
        self.assertEqual(reference.date_source, 'drive_photo_time')

    def test_generic_exif_date_is_marked_as_possible_edit(self):
        _, reference = media.prepare_photo(dated_jpeg(original=None), AnalysisSettings())
        self.assertEqual(reference.date_source, 'exif_datetime')
        self.assertIn('edit rather than capture', reference.note)

    def test_only_selected_photo_metadata_prepared(self):
        files = [dict(id='chosen', name='private.jpg', size='1000', kind='image', mimeType='image/jpeg',
                      imageMediaMetadata={'time': '2024-04-01T10:00:00Z'}, modifiedTime='2026-01-01T00:00:00Z')]
        with patch.object(media.media, 'download_media', return_value=dated_jpeg(original=None)):
            images, mapping = media.prepare_selected_media(None, files, AnalysisSettings())
        self.assertEqual(images[0].photo_date.reference_date, '2024-04-01')
        payload = json.dumps(ai.build_payload(AnimalSources(), images, AnalysisSettings()))
        self.assertIn('2024-04-01', payload)
        self.assertNotIn('private.jpg', payload)
        self.assertNotIn('chosen', payload)
        self.assertNotIn('PRIVATE DEVICE', payload)
        self.assertNotIn('2026-01-01', payload)

    def test_date_change_marks_fingerprint_different(self):
        file = dict(id='photo', name='p.jpg', kind='image', imageMediaMetadata={'time': '2024-05-02T10:00:00Z'})
        before = ui.input_fingerprint(AnimalSources(), [file], {}, AnalysisSettings())
        after = ui.input_fingerprint(AnimalSources(), [dict(file, imageMediaMetadata={'time': '2024-06-02T10:00:00Z'})], {}, AnalysisSettings())
        self.assertNotEqual(before, after)


class AgeTests(unittest.TestCase):
    def data(self, evidence=None, **age_values):
        data = empty_profile().model_dump()
        data['evidence'] = evidence or []
        data['age_assessment'] = dict(estimate='Possibly a puppy at the photo date.', reference_date='2024-05-02',
            photo_reference='photo:1', basis_evidence_ids=['e1'], status='qualified_visual_estimate',
            limitations='A rough visual impression only; confirm with rescuer.')
        data['age_assessment'].update(age_values)
        return data

    def test_photo_timestamp_alone_keeps_age_unknown(self):
        profile = ai.validate_profile(empty_profile(), AnimalSources(), [selected_photo()])
        self.assertEqual(profile.photo_date_references[0].reference_date, '2024-05-02')
        self.assertEqual(profile.age_assessment.status, 'unknown')
        self.assertIsNone(profile.age_assessment.estimate)

    def test_qualified_visual_life_stage_has_dated_photo_and_warning(self):
        evidence = record(ref='photo:1', quote='Small puppy-like proportions.',
            verification_status='interpretation_requires_review', temporal_status='at_sampled_moment')
        profile = ai.validate_profile(AnimalProfile.model_validate(self.data([evidence])), AnimalSources(), [selected_photo()])
        self.assertEqual(profile.age_assessment.status, 'qualified_visual_estimate')
        self.assertIn('age_uncertainty', [warning.category for warning in profile.review_warnings])

    def test_date_only_evidence_cannot_establish_age(self):
        evidence = record(ref='photo:1#date', quote='2024:05:02 10:11:12', source_type='PHOTO_DATE_METADATA',
            verification_status='metadata_not_independently_verified', temporal_status='at_sampled_moment')
        with self.assertRaises(ValueError):
            ai.validate_profile(AnimalProfile.model_validate(self.data([evidence])), AnimalSources(), [selected_photo()])

    def test_wrong_date_unselected_photo_and_unqualified_impression_rejected(self):
        evidence = record(ref='photo:1', quote='Puppy-like proportions.',
            verification_status='interpretation_requires_review', temporal_status='at_sampled_moment')
        for field, value in [('reference_date', '2025-01-01'), ('photo_reference', 'photo:2')]:
            data = self.data([evidence])
            data['age_assessment'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ai.validate_profile(AnimalProfile.model_validate(data), AnimalSources(), [selected_photo()])
        evidence['verification_status'] = 'direct_visual_observation'
        with self.assertRaises(ValueError):
            ai.validate_profile(AnimalProfile.model_validate(self.data([evidence])), AnimalSources(), [selected_photo()])

    def test_model_photo_dates_and_rescuer_are_replaced_by_real_input_context(self):
        data = empty_profile().model_dump()
        data['photo_date_references'] = [dict(selected_photo().photo_date.model_dump(), reference_date='1990-01-01')]
        data['source_attribution'] = dict(rescuer_name='Invented', source_reference='rescuer_attribution',
                                         provided_sections=['history'], verification_note='Invented')
        profile = ai.validate_profile(AnimalProfile.model_validate(data), AnimalSources(), [selected_photo()], AnimalContext(rescuer_name='Sophie'))
        self.assertEqual(profile.photo_date_references[0].reference_date, '2024-05-02')
        self.assertEqual(profile.source_attribution.rescuer_name, 'Sophie')


@isolated_editorial_storage
class AttributionUiTests(unittest.TestCase):
    def test_selected_label_displays_dog_and_rescuer_separately_and_passes_context(self):
        with (patch.object(ui, 'load_settings', return_value=AnalysisSettings(model='synthetic')),
             patch.object(ui, 'require_credentials', return_value='synthetic-key'),
             patch.object(ui, 'prepare_selected_media', return_value=([], {})),
             patch.object(ai, 'analyze_animal', return_value=empty_profile()) as analyze):
            app = AppTest.from_file(APP, default_timeout=10).run()
            app.session_state['media_animal'] = {'id': 'azra', 'name': 'Azra (Sophie)'}
            app.text_area(key='animal_history').input('Azra was rescued.').run()
            self.assertFalse(app.exception)
            self.assertTrue(any(item.value == 'Selected animal: Azra' for item in app.text))
            self.assertTrue(any(item.value == 'Rescuer: Sophie' for item in app.text))
            app.checkbox(key='ai_consent').check().run()
            app.button(key='analyze_animal').click().run()
            self.assertEqual(analyze.call_args.kwargs['context'], AnimalContext(animal_name='Azra', rescuer_name='Sophie'))
            self.assertFalse(app.exception)


if __name__ == '__main__':
    unittest.main()

import io
import json
from unittest.mock import patch
from xml.sax.saxutils import escape

import requests
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from . import paper_search as search
from . import utils_ai as ai
from .models import ResearchPoster

SKIN = 'Skin-R1: Clinical Knowledge-Guided Dermatological Diagnosis Using Vision-Language Models'
PHENO_POSTER = 'PhenoLIP: Phenotype Guided Medical Vision-Language Pretraining'
PHENO_PAPER = 'PhenoLIP: Integrating Phenotype Ontology Knowledge into Medical Vision-Language Pretraining'


def response(body='', status=200, content_type='text/html', **headers):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode() if isinstance(body, str) else body
    result._content_consumed = True
    result.headers.update({'Content-Type': content_type, **headers})
    result.url = 'https://example.org/'
    return result


def paper(title=SKIN, identifier='2511.14900'):
    return {
        'title': title, 'paper_url': f'https://arxiv.org/abs/{identifier}',
        'pdf_url': f'https://arxiv.org/pdf/{identifier}', 'arxiv_id': identifier,
        'authors': 'Zehao Liu, Weijieying Ren', 'year': 2025, 'abstract': 'Verified abstract.',
        'doi': '', '_blocked': False, 'source': 'arxiv',
    }


def feed(title=SKIN, identifier='2511.14900v2'):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
    <entry><id>http://arxiv.org/abs/{identifier}</id><title>{escape(title)}</title>
    <published>2025-11-18T20:38:36Z</published><summary>Verified abstract.</summary>
    <author><name>Zehao Liu</name></author><author><name>Weijieying Ren</name></author>
    <arxiv:doi>10.1234/test</arxiv:doi>
    <link href="https://arxiv.org/pdf/{identifier}" title="pdf" type="application/pdf"/>
    </entry></feed>'''


class PaperLookupTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.http = self.enterContext(patch.object(search.requests, 'get', side_effect=AssertionError('Unexpected network request')))
        self.enterContext(patch.object(search.time, 'sleep'))
        self.enterContext(patch.object(search, '_wait_for_arxiv', return_value=True))

    def test_arxiv_identifiers_and_versions_are_normalized(self):
        for value in ('2511.14900v2', 'arXiv:2511.14900',
                      'https://arxiv.org/abs/2511.14900v2',
                      'http://export.arxiv.org/pdf/2511.14900v2.pdf',
                      'https://arxiv.org/html/2511.14900v2',
                      'https://doi.org/10.48550/arXiv.2511.14900'):
            with self.subTest(value=value):
                self.assertEqual(search._arxiv_id(value), '2511.14900')
        self.assertEqual(search._arxiv_id('https://arxiv.org/pdf/hep-th/9901001v2.pdf'), 'hep-th/9901001')

    def test_arxiv_lookalike_hosts_and_invalid_ids_are_rejected(self):
        for value in ('https://arxiv.org.evil.test/abs/2511.14900',
                      'https://evil.test/arxiv.org/abs/2511.14900',
                      'https://arxiv.org@evil.test/abs/2511.14900', '2511.1490000', ''):
            self.assertEqual(search._arxiv_id(value), '')

    def test_changed_phenolip_subtitle_matches_but_unrelated_work_does_not(self):
        self.assertGreater(search._match_score(PHENO_POSTER, PHENO_PAPER), 0)
        self.assertEqual(search._match_score(PHENO_POSTER, 'PhenoLIP: Organic pollution and soil composition'), 0)
        self.assertEqual(search._match_score(SKIN, SKIN.replace('Skin-R1', 'Skin-R2')), 0)
        self.assertEqual(search._match_score(SKIN, 'Vision Language Models'), 0)

    def test_exact_candidate_wins_over_partial_match_and_punctuation_is_normalized(self):
        exact = paper()
        related = paper(SKIN.replace('Diagnosis', 'Classification'), '2501.12345')
        self.assertEqual(search._best_match(SKIN, [related, exact]), exact)
        self.assertEqual(search._match_score(SKIN, '[PDF] ' + SKIN.replace('-', '–')), 1)

    def test_arxiv_returns_full_metadata_and_caches_success(self):
        self.http.side_effect = None
        self.http.return_value = response(feed(), content_type='application/atom+xml')
        result = search._search_arxiv(SKIN)[0]
        self.assertEqual(result['paper_url'], 'https://arxiv.org/abs/2511.14900')
        self.assertEqual(result['pdf_url'], 'https://arxiv.org/pdf/2511.14900')
        self.assertEqual(result['authors'], 'Zehao Liu, Weijieying Ren')
        self.assertEqual(result['year'], 2025)
        self.assertEqual(result['doi'], '10.1234/test')
        self.assertEqual(search._search_arxiv(SKIN)[0], result)
        self.assertEqual(self.http.call_count, 1)

    def test_arxiv_search_includes_acronym_for_changed_subtitles(self):
        with patch.object(search, '_arxiv_feed', return_value=[paper(PHENO_PAPER, '2602.06184')]) as request:
            result = search.search_paper(PHENO_POSTER)
        self.assertEqual(result['arxiv_id'], '2602.06184')
        self.assertIn('ti:"PhenoLIP"', request.call_args.args[0]['search_query'])
        self.http.assert_not_called()

    def test_native_arxiv_success_does_not_depend_on_other_providers(self):
        with patch.object(search, '_search_arxiv', return_value=[paper()]), \
             patch.object(search, '_search_semantic_scholar') as semantic, \
             patch.object(search, '_search_google_scholar') as google:
            self.assertEqual(search.search_paper(SKIN)['arxiv_id'], '2511.14900')
        semantic.assert_not_called()
        google.assert_not_called()

    def test_invalid_xml_is_not_cached_as_missing_paper(self):
        self.http.side_effect = [response('broken XML'), response(feed())]
        params = {'id_list': '2511.14900'}
        self.assertEqual(search._arxiv_feed(params), [])
        self.assertEqual(search._arxiv_feed(params)[0]['arxiv_id'], '2511.14900')

    def test_api_outage_falls_back_to_arxiv_web_search(self):
        self.http.side_effect = None
        self.http.return_value = response(f'''<li class="arxiv-result"><p class="list-title">
            <a href="https://arxiv.org/abs/2602.06184">arXiv:2602.06184</a></p>
            <p class="title">{PHENO_PAPER}</p><p class="authors"><a>Cheng Liang</a></p></li>''')
        with patch.object(search, '_arxiv_feed', return_value=[]):
            found = search.search_paper(PHENO_POSTER)
        self.assertEqual(found['paper_url'], 'https://arxiv.org/abs/2602.06184')
        self.assertEqual(found['source'], 'arxiv_web')

    def test_direct_id_uses_abstract_page_when_api_is_down(self):
        self.http.side_effect = None
        self.http.return_value = response(f'<meta name="citation_title" content="{SKIN}"><meta name="citation_author" content="Zehao Liu">')
        with patch.object(search, '_arxiv_feed', return_value=[]):
            found = search.search_paper(SKIN, arxiv_id='2511.14900v2')
        self.assertEqual(found['paper_url'], 'https://arxiv.org/abs/2511.14900')

    def test_wrong_id_extracted_from_image_does_not_attach_wrong_paper(self):
        with patch.object(search, '_get_arxiv_paper', return_value=paper('Unrelated computer architecture', '2401.12345')), \
             patch.object(search, '_search_arxiv', return_value=[paper()]):
            self.assertEqual(search.search_paper(SKIN, arxiv_id='2401.12345')['arxiv_id'], '2511.14900')

    @override_settings(SEMANTIC_SCHOLAR_API_KEY='test-key')
    def test_rejected_semantic_key_falls_back_to_public_access(self):
        payload = {'data': [{'title': SKIN, 'externalIds': {'ArXiv': '2511.14900'}, 'authors': []}]}
        self.http.side_effect = [response(status=403), response(json.dumps(payload))]
        with self.assertLogs(search.logger, level='WARNING') as logs:
            found = search._search_semantic_scholar(SKIN)
        self.assertEqual(found[0]['paper_url'], 'https://arxiv.org/abs/2511.14900')
        self.assertIn('x-api-key', self.http.call_args_list[0].kwargs['headers'])
        self.assertNotIn('x-api-key', self.http.call_args_list[1].kwargs['headers'])
        self.assertNotIn('test-key', '\n'.join(logs.output))

    def test_timeouts_and_rate_limits_have_bounded_retries(self):
        self.http.side_effect = [requests.Timeout(), response(status=429, **{'Retry-After': '120'})]
        with self.assertLogs(search.logger, level='WARNING'):
            self.assertIsNone(search._request_search('test', 'https://example.org'))
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(search.time.sleep.call_args.args, (3,))

    def test_retry_after_is_capped_and_server_failure_can_recover(self):
        self.http.side_effect = [response(status=503, **{'Retry-After': '120'}), response('OK')]
        self.assertIsNotNone(search._request_search('test', 'https://example.org'))
        search.time.sleep.assert_called_once_with(10)

    def test_google_challenge_does_not_count_as_a_search_result(self):
        self.http.side_effect = None
        self.http.return_value = response('<form action="/sorry/Captcha"><div class="gs_r gs_or">blocked</div></form>')
        with self.assertLogs(search.logger, level='WARNING'):
            self.assertEqual(search._search_google_scholar(SKIN), [])

    def test_google_pdf_result_is_promoted_to_canonical_arxiv_link(self):
        self.http.side_effect = None
        self.http.return_value = response(f'''<div class="gs_r gs_or"><div class="gs_ri"><h3 class="gs_rt">
            <a href="https://arxiv.org/pdf/2511.14900v2.pdf">{SKIN}</a></h3></div></div>''')
        self.assertEqual(search._search_google_scholar(SKIN)[0]['paper_url'], 'https://arxiv.org/abs/2511.14900')

    def test_other_providers_remain_available_after_arxiv_and_semantic_fail(self):
        with patch.object(search, '_search_arxiv', return_value=[]), \
             patch.object(search, '_search_semantic_scholar', return_value=[]), \
             patch.object(search, '_search_google_scholar', return_value=[paper()]):
            self.assertEqual(search.search_paper(SKIN)['arxiv_id'], '2511.14900')

    def test_short_google_query_is_quoted_and_checked_against_full_title(self):
        def google(query):
            return [paper(PHENO_PAPER, '2602.06184')] if query == '"PhenoLIP"' else []
        with patch.object(search, '_search_arxiv', return_value=[]), \
             patch.object(search, '_search_semantic_scholar', return_value=[]), \
             patch.object(search, '_search_google_scholar', side_effect=google):
            self.assertEqual(search.search_paper(PHENO_POSTER)['arxiv_id'], '2602.06184')

    def test_unrelated_results_from_all_providers_are_rejected(self):
        wrong = [paper('A survey of computer networks')]
        with patch.object(search, '_search_arxiv', return_value=wrong), \
             patch.object(search, '_search_semantic_scholar', return_value=wrong), \
             patch.object(search, '_search_google_scholar', return_value=wrong):
            self.assertIsNone(search.search_paper(SKIN))

    def test_repository_references_are_validated_before_attaching(self):
        self.http.side_effect = None
        self.http.return_value = response('''<article class="markdown-body">
            <a href="https://arxiv.org/abs/2401.12345">Cited work</a>
            <a href="https://arxiv.org/abs/2602.06184">Our paper</a></article>''')
        with patch.object(search, '_get_arxiv_paper', side_effect=[paper('Unrelated work', '2401.12345'), paper(PHENO_PAPER, '2602.06184')]):
            found = search.find_paper_from_github('https://github.com/MAGIC-AI4Med/PhenoLIP', PHENO_POSTER)
        self.assertEqual(found['arxiv_id'], '2602.06184')

    def test_repository_fallback_rejects_non_github_hosts(self):
        self.assertIsNone(search.find_paper_from_github('https://github.com.evil.test/org/repo', SKIN))
        self.http.assert_not_called()


class EnrichmentTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch.object(ai.requests, 'get', side_effect=AssertionError('Unexpected network request')))
        self.info = {'is_research_poster': True, 'title': SKIN, 'search_query': SKIN + ' Zhehao Liu', 'authors': 'Misread Name'}
        self.enterContext(patch.object(ai, 'extract_poster_info', return_value=self.info))
        self.lookup = self.enterContext(patch.object(ai, 'search_paper', return_value=None))
        self.pdf = self.enterContext(patch.object(ai, '_find_real_pdf', return_value=''))
        self.github = self.enterContext(patch.object(ai, 'find_github_repo', return_value=''))
        self.repo_paper = self.enterContext(patch.object(ai, 'find_paper_from_github', return_value=None))
        self.linked_authors = self.enterContext(patch.object(ai, 'fetch_authors', return_value='Verified Paper Author'))
        self.enterContext(patch.object(ai, '_generate_description_from_pdf', return_value='PDF summary'))
        self.enterContext(patch.object(ai, '_scrape_description_from_site', return_value='Page summary'))
        self.enterContext(patch.object(ai, '_generate_description_from_poster', return_value='Poster summary'))

    def test_pdf_only_arxiv_fallback_populates_saved_link_and_year(self):
        self.pdf.return_value = 'https://arxiv.org/pdf/2511.14900v2.pdf'
        result = ai.analyze_and_enrich('unused')
        self.assertEqual(result['paper_link'], 'https://arxiv.org/abs/2511.14900')
        self.assertEqual(result['publication_year'], 2025)
        self.assertEqual(self.github.call_args.kwargs['paper_url'], result['paper_link'])

    def test_non_arxiv_pdf_remains_accessible_when_no_landing_page_is_found(self):
        self.pdf.return_value = 'https://publisher.example/paper.pdf'
        self.assertEqual(ai.analyze_and_enrich('unused')['paper_link'], self.pdf.return_value)

    def test_verified_metadata_replaces_misread_authors(self):
        self.lookup.return_value = paper()
        result = ai.analyze_and_enrich('unused')
        self.assertEqual(result['authors'], 'Zehao Liu, Weijieying Ren')
        self.assertEqual(result['publication_year'], 2025)
        self.assertEqual(self.lookup.call_args.args, (SKIN,))

    def test_scholar_link_without_author_metadata_overrides_wrong_image_names(self):
        self.lookup.return_value = {**paper(), 'authors': '', 'source': 'google_scholar'}
        self.linked_authors.return_value = 'Cheng Liang, Chaoyi Wu, Weike Zhao, Ya Zhang, Yanfeng Wang, Weidi Xie'
        result = ai.analyze_and_enrich('unused')
        self.assertEqual(result['authors'], self.linked_authors.return_value)
        self.linked_authors.assert_called_once_with('https://arxiv.org/abs/2511.14900', title=SKIN)

    def test_pdf_only_fallback_verifies_authors_despite_nonempty_image_names(self):
        self.pdf.return_value = 'https://arxiv.org/pdf/2511.14900v2.pdf'
        self.assertEqual(ai.analyze_and_enrich('unused')['authors'], 'Verified Paper Author')

    def test_metadata_outage_preserves_image_authors_as_fallback(self):
        self.lookup.return_value = {**paper(), 'authors': ''}
        self.linked_authors.return_value = ''
        self.assertEqual(ai.analyze_and_enrich('unused')['authors'], 'Misread Name')

    def test_github_recovery_populates_paper_and_description_inputs(self):
        self.github.return_value = 'https://github.com/MAGIC-AI4Med/PhenoLIP'
        self.repo_paper.return_value = paper(PHENO_PAPER, '2602.06184')
        result = ai.analyze_and_enrich('unused')
        self.assertEqual(result['paper_link'], 'https://arxiv.org/abs/2602.06184')
        self.assertEqual(result['summary'], 'PDF summary')

    def test_manual_paper_and_github_links_take_precedence(self):
        overrides = {'paper_link': 'https://example.org/my-paper', 'github_link': 'https://github.com/org/manual'}
        result = ai.analyze_and_enrich('unused', overrides=overrides)
        self.assertEqual(result['paper_link'], overrides['paper_link'])
        self.assertEqual(result['github_link'], overrides['github_link'])
        self.lookup.assert_not_called()
        self.github.assert_not_called()

    def test_provider_failure_keeps_poster_analysis_available(self):
        result = ai.analyze_and_enrich('unused')
        self.assertEqual(result['title'], SKIN)
        self.assertEqual(result['summary'], 'Poster summary')
        self.assertEqual(result['paper_link'], '')


class PDFValidationTests(SimpleTestCase):
    def setUp(self):
        ai._clear_pdf_cache()
        self.addCleanup(ai._clear_pdf_cache)

    def test_pdf_extension_does_not_bypass_404_or_html_validation(self):
        with patch.object(ai.requests, 'head', return_value=response(status=404)), \
             patch.object(ai.requests, 'get', return_value=response('Not found', status=404)):
            self.assertFalse(ai._is_valid_pdf_url('https://example.org/missing.pdf'))
        with patch.object(ai.requests, 'head', return_value=response('HTML')), \
             patch.object(ai.requests, 'get', return_value=response('HTML')):
            self.assertFalse(ai._is_valid_pdf_url('https://example.org/login.pdf'))

    def test_get_can_validate_pdf_when_head_times_out(self):
        with patch.object(ai.requests, 'head', side_effect=requests.Timeout), \
             patch.object(ai.requests, 'get', return_value=response(b'%PDF-1.7', content_type='application/pdf')):
            self.assertTrue(ai._is_valid_pdf_url('https://example.org/paper.pdf'))

    def test_arxiv_pdf_fallback_rejects_unrelated_first_result(self):
        with patch.object(ai, '_search_arxiv', return_value=[paper('Unrelated paper')]):
            self.assertEqual(ai._find_pdf_via_arxiv(SKIN), '')

    def test_download_checks_actual_bytes_even_with_malformed_content_length(self):
        with patch.object(ai.requests, 'get', return_value=response(b'12345', **{'Content-Length': 'invalid'})), \
             patch.object(ai, 'MAX_PDF_BYTES', 4):
            self.assertIsNone(ai._download_pdf_safe('https://example.org/large.pdf'))

    def test_declared_oversize_is_refused_before_reading_the_body(self):
        with patch.object(ai.requests, 'get', return_value=response(b'%PDF-1.7', **{'Content-Length': '999999999'})), \
             patch.object(ai, 'MAX_PDF_BYTES', 1024):
            self.assertIsNone(ai._download_pdf_safe('https://example.org/huge.pdf'))

    def test_the_same_pdf_is_downloaded_once_per_analysis(self):
        pdf = response(b'%PDF-1.7 body', content_type='application/pdf')
        with patch.object(ai.requests, 'get', return_value=pdf) as http:
            first = ai._download_pdf_safe('https://example.org/paper.pdf')
            second = ai._download_pdf_safe('https://example.org/paper.pdf')
        self.assertEqual(first, second)
        self.assertEqual(http.call_count, 1)
        ai._clear_pdf_cache()
        with patch.object(ai.requests, 'get', return_value=pdf) as http:
            ai._download_pdf_safe('https://example.org/paper.pdf')
        self.assertEqual(http.call_count, 1)

    def test_a_payload_that_is_not_a_pdf_is_never_parsed(self):
        with patch.object(ai.requests, 'get', return_value=response(b'GIF89a not a pdf', content_type='image/gif')):
            self.assertIsNone(ai._read_pdf('https://example.org/decoy.pdf'))
        ai._clear_pdf_cache()
        with patch.object(ai.requests, 'get', return_value=response('<html>paywall</html>')):
            self.assertEqual(ai._extract_text_from_pdf('https://example.org/paywall.pdf'), '')

    def test_extracted_pdf_text_is_capped(self):
        class _Page:
            def extract_text(self):
                return 'x' * 50_000

        class _Reader:
            pages = [_Page() for _ in range(40)]

        with patch.object(ai, 'MAX_PDF_TEXT_CHARS', 10_000):
            self.assertEqual(len(ai._pdf_text(_Reader())), 10_000)


class GitHubLookupTests(SimpleTestCase):
    @override_settings(GITHUB_TOKEN='top-secret-token')
    def test_repository_search_authenticates_without_leaking_the_token(self):
        payload = {'items': [{'name': 'phenolip', 'html_url': 'https://github.com/org/PhenoLIP'}]}
        with patch.object(ai.requests, 'get', return_value=response(json.dumps(payload))) as http, \
             self.assertLogs(ai.logger, level='INFO') as logs:
            ai.logger.info('search starting')
            found = ai._search_github_api('PhenoLIP: a study', github_query='PhenoLIP')
        self.assertEqual(found, 'https://github.com/org/PhenoLIP')
        self.assertEqual(http.call_args.kwargs['headers']['Authorization'], 'Bearer top-secret-token')
        self.assertNotIn('top-secret-token', '\n'.join(logs.output))

    @override_settings(GITHUB_TOKEN='')
    def test_repository_search_works_unauthenticated(self):
        payload = {'items': [{'name': 'phenolip', 'html_url': 'https://github.com/org/PhenoLIP'}]}
        with patch.object(ai.requests, 'get', return_value=response(json.dumps(payload))) as http:
            ai._search_github_api('PhenoLIP: a study', github_query='PhenoLIP')
        self.assertNotIn('Authorization', http.call_args.kwargs['headers'])

    def test_search_is_skipped_when_the_name_is_absent_from_the_title(self):
        with patch.object(ai.requests, 'get', side_effect=AssertionError('Unexpected network request')):
            self.assertEqual(ai._search_github_api('A study of networks', github_query='phenolip'), '')
            self.assertEqual(ai._search_github_api('', github_query='phenolip'), '')
            self.assertEqual(ai._search_github_api(SKIN, github_query=''), '')

    def test_unrelated_repository_names_are_rejected(self):
        payload = {'items': [{'name': 'phenolip-fork-2', 'html_url': 'https://github.com/other/phenolip-fork-2'}]}
        with patch.object(ai.requests, 'get', return_value=response(json.dumps(payload))):
            self.assertEqual(ai._search_github_api('PhenoLIP: a study', github_query='PhenoLIP'), '')

    def test_api_errors_and_malformed_results_degrade_quietly(self):
        for reply in (response(status=403), response(status=500), response('not json'), response('{"items": 3}')):
            with self.subTest(status=reply.status_code):
                with patch.object(ai.requests, 'get', return_value=reply):
                    self.assertEqual(ai._search_github_api('PhenoLIP: a study', github_query='PhenoLIP'), '')
        with patch.object(ai.requests, 'get', side_effect=requests.Timeout):
            self.assertEqual(ai._search_github_api('PhenoLIP: a study', github_query='PhenoLIP'), '')


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class PaperPersistenceTests(TestCase):
    def setUp(self):
        self.poster = ResearchPoster.objects.create(
            title=SKIN, authors='Author', summary='Original summary', image='posters/test.jpg',
            image_sha256='a' * 64, paper_link='https://arxiv.org/abs/2511.14900',
            ai_paper_link='https://arxiv.org/abs/2511.14900',
            github_link='https://github.com/org/repo', ai_github_link='https://github.com/org/repo',
        )

    def test_retry_does_not_erase_previously_found_links(self):
        from .views import process_uploaded_poster
        with patch('bot_engine.views.analyze_and_enrich', return_value={
            'is_research_poster': True, 'title': SKIN, 'summary': 'New summary',
            'paper_link': '', 'github_link': '',
        }):
            _, _, error = process_uploaded_poster(None, None, existing_poster=self.poster)
        self.assertIsNone(error)
        self.poster.refresh_from_db()
        self.assertEqual(self.poster.paper_link, 'https://arxiv.org/abs/2511.14900')
        self.assertEqual(self.poster.github_link, 'https://github.com/org/repo')

    def test_manual_override_remains_manual_across_consecutive_retries(self):
        from .views import process_uploaded_poster
        self.poster.paper_link = 'https://example.org/manually-chosen'
        self.poster.github_link = 'https://github.com/org/manual'
        self.poster.save()
        def enrich(path, overrides):
            self.assertEqual(overrides['paper_link'], 'https://example.org/manually-chosen')
            self.assertEqual(overrides['github_link'], 'https://github.com/org/manual')
            return {'is_research_poster': True, 'title': SKIN, 'summary': 'Summary', **overrides}
        with patch('bot_engine.views.analyze_and_enrich', side_effect=enrich):
            for _ in range(2):
                _, _, error = process_uploaded_poster(None, None, existing_poster=self.poster)
                self.assertIsNone(error)
                self.poster.refresh_from_db()
                self.assertEqual(self.poster.ai_paper_link, 'https://arxiv.org/abs/2511.14900')
                self.assertEqual(self.poster.ai_github_link, 'https://github.com/org/repo')

    def test_repair_previews_then_saves_only_missing_links(self):
        self.poster.paper_link = ''
        self.poster.ai_paper_link = ''
        self.poster.validation_status = 'approved'
        self.poster.save()
        with patch('bot_engine.management.commands.repair_paper_links.search_paper', return_value=paper()):
            call_command('repair_paper_links', str(self.poster.pk), stdout=io.StringIO())
            self.poster.refresh_from_db()
            self.assertEqual(self.poster.paper_link, '')
            call_command('repair_paper_links', str(self.poster.pk), apply=True, stdout=io.StringIO())
        self.poster.refresh_from_db()
        self.assertEqual(self.poster.paper_link, 'https://arxiv.org/abs/2511.14900')
        self.assertEqual(self.poster.ai_paper_link, self.poster.paper_link)
        self.assertEqual(self.poster.validation_status, 'approved')
        self.assertEqual(self.poster.summary, 'Original summary')

    def test_repair_skips_existing_links_and_active_analyses(self):
        with patch('bot_engine.management.commands.repair_paper_links.search_paper') as lookup:
            call_command('repair_paper_links', str(self.poster.pk), apply=True, stdout=io.StringIO())
            self.poster.paper_link = ''
            self.poster.analysis_status = 'processing'
            self.poster.save()
            call_command('repair_paper_links', str(self.poster.pk), apply=True, stdout=io.StringIO())
        lookup.assert_not_called()

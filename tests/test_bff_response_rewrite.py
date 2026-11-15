"""Protected capabilities keep their public prefix and bounded rewrite scope."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import httpx
from fastapi import Request

from examples.bff.proxy_responses import rewrite_response


class ResponseRewriteTests(unittest.TestCase):
    def request(self, path):
        return Request({
            'type': 'http', 'scheme': 'https', 'server': ('site.test', 443),
            'path': path, 'query_string': b'', 'headers': [],
        })

    def rewrite(self, value, service, suffix='', *, status=200):
        request = self.request('/tenant/api/bff/' + service + '/' + suffix)
        upstream = httpx.Response(status, json=value)
        deployment = SimpleNamespace(api_base_url='https://public.test/tenant/api')
        body, changed = rewrite_response(upstream, request, deployment, service, suffix)
        return json.loads(body), changed

    def test_nested_media_rewrites_only_recognised_fields_and_endpoint_paths(self):
        payload = {
            'items': [
                {
                    'image_url': 'http://internal/get_violation_image?id=7#discard',
                    'thumbnail_url': '/get_violation_thumbnail?id=7',
                },
                {'image_url': '/other', 'description': '/get_violation_image?id=8'},
            ],
        }
        rewritten, changed = self.rewrite(payload, 'violations', status=404)
        self.assertTrue(changed)
        self.assertEqual(
            rewritten['items'][0], {
                'image_url': 'https://public.test/tenant/api/bff/violations/get_violation_image?id=7',
                'thumbnail_url': 'https://public.test/tenant/api/bff/violations/get_violation_thumbnail?id=7',
            },
        )
        self.assertEqual(rewritten['items'][1], payload['items'][1])

    def test_download_rewrite_requires_success_exact_route_and_relative_capability(self):
        for service in ('files', 'file_manage'):
            for source in ('/download/temp/a.b.c', '/tenant/api/file_manage/download/temp/a.b.c'):
                value, changed = self.rewrite({'temp_url': source}, service, 'versions/7/generate_temp_url')
                self.assertTrue(changed)
                self.assertEqual(
                    value['temp_url'], 'https://public.test/tenant/api/bff/' +
                    service + '/download/temp/a.b.c',
                )
            for source in (
                'https://other.test/download/temp/a.b.c', '/download/temp/a.b.c?extra=1',
                '/download/temp/not-a-token',
            ):
                value = {'temp_url': source}
                self.assertEqual(self.rewrite(value, service, 'exports/7/generate_temp_url'), (value, False))
            value = {'temp_url': '/download/temp/a.b.c'}
            self.assertEqual(self.rewrite(value, service, 'versions/7/generate_temp_url', status=400), (value, False))
            self.assertEqual(self.rewrite(value, service, 'other'), (value, False))

    def test_binary_invalid_json_and_irrelevant_services_keep_exact_bytes(self):
        request = self.request('/bff/violations/get_violation_image')
        for service, content, content_type in (
            ('violations', b'\xffimage', 'image/jpeg'),
            ('violations', b'{invalid}', 'application/json'),
            ('violations', b'{  "image_url": "/other"  }', 'application/json'),
            ('other', b'{  "image_url": "/get_violation_image"  }', 'application/json'),
        ):
            with self.subTest(service=service, content=content):
                upstream = httpx.Response(200, content=content, headers={'content-type': content_type})
                self.assertEqual(rewrite_response(upstream, request, None, service, ''), (content, False))

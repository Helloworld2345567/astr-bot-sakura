"""Site URL routing and redirect boundaries; no external requests."""
import importlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from test_sakura_runtime_context import Event, plugin
from astrbot.core.tools.web_search_tools import TavilyExtractWebPageTool

from support import web


class SiteReadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_zhihu_hook_upgrades_basic_and_preserves_member(self):
        main = plugin.Main(SimpleNamespace())
        event = Event('阅读知乎')
        args = {'url':'https://www.zhihu.com/question/462589216','extract_depth':'basic'}
        await main.enforce_sakura_tool_permissions(event,TavilyExtractWebPageTool(),args)
        self.assertEqual(args['extract_depth'],'advanced')
        self.assertEqual(event.role,'member')

    async def test_short_url_expands(self):
        args = {'url':'https://v.douyin.com/abc/'}
        with patch.object(web,'_resolve_douyin_short_url',AsyncMock(return_value='https://www.douyin.com/video/123')):
            await web.prepare_extract_args(args)
        self.assertEqual(args,{'url':'https://www.douyin.com/video/123','extract_depth':'advanced'})

    async def test_failed_resolution_keeps_input(self):
        args = {'url':'https://v.douyin.com/abc/'}
        with patch.object(web,'_resolve_douyin_short_url',AsyncMock(side_effect=TimeoutError)):
            await web.prepare_extract_args(args)
        self.assertEqual(args['url'],'https://v.douyin.com/abc/')

    async def test_official_share_url_normalizes_without_fetching_tracking_url(self):
        args={'url':'https://www.iesdouyin.com/share/video/123/?tracking=example'}
        await web.prepare_extract_args(args)
        self.assertEqual(args['url'],'https://www.douyin.com/video/123')

    async def test_other_sites_and_fake_tool_unchanged(self):
        for url in ('https://example.com','https://zhihu.com.example.org','https://douyin.com.example.org'):
            args={'url':url}
            await web.prepare_extract_args(args)
            self.assertEqual(args,{'url':url})

    async def test_redirect_cannot_access_other_hosts(self):
        response=MagicMock(status=302,headers={'Location':'http://127.0.0.1:6185/'})
        response.__aenter__=AsyncMock(return_value=response)
        response.__aexit__=AsyncMock(return_value=False)
        session=MagicMock()
        session.get.return_value=response
        manager=MagicMock()
        manager.__aenter__=AsyncMock(return_value=session)
        manager.__aexit__=AsyncMock(return_value=False)
        with patch.object(web.aiohttp,'ClientSession',return_value=manager):
            result=await web._resolve_douyin_short_url('https://v.douyin.com/abc/')
        self.assertEqual(result,'https://v.douyin.com/abc/')
        session.get.assert_called_once_with('https://v.douyin.com/abc/',allow_redirects=False)

    async def test_redirect_validation(self):
        for url in ('https://v.douyin.com:8888/','https://user@v.douyin.com/','http://v.douyin.com/','https://v.douyin.com.example.org/'):
            self.assertFalse(web._allowed_douyin_url(url))


if __name__=='__main__':unittest.main()

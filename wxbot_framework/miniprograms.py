"""小程序通用业务接口：组件树、引擎内点击、文字输入与截图。

页面内容只返回给调用者，不写入消息监控或运行日志。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import math
import re
import threading
import uuid


@dataclass(frozen=True)
class MiniProgramPage:
    app_id: str
    route: str
    page_id: int
    session_id: str = ''


@dataclass(frozen=True)
class MiniProgramComponent:
    """组件的公开渲染信息；xpath 相对于所在 frame，不是系统控件索引。"""
    tag: str
    name: str
    text: str
    element_id: str
    classes: str
    role: str
    placeholder: str
    disabled: bool
    xpath: str
    page: MiniProgramPage


class _ComponentParser(HTMLParser):
    _VOID = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                       'link', 'meta', 'param', 'source', 'track', 'wbr'})
    _SKIP = frozenset({'script', 'style', 'template'})
    _ATTRIBUTES = frozenset({'id', 'class', 'name', 'role', 'aria-label', 'placeholder',
                            'disabled', 'aria-disabled', 'type'})
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.page, self.stack, self.nodes = page, [], []
        self.roots = {}
    def handle_starttag(self, tag, attrs):
        parent = self.stack[-1] if self.stack else None
        siblings = parent['children'] if parent else self.roots
        index = siblings[tag] = siblings.get(tag, 0) + 1
        path = (parent['path'] if parent else '') + '/' + tag + '[' + str(index) + ']'
        skipped = tag in self._SKIP or bool(parent and parent['skip'])
        private_input = tag in ('input', 'textarea', 'wx-input', 'wx-textarea') or bool(
            parent and parent['private_input'])
        node = {'tag': tag, 'attrs': {key: value or '' for key, value in attrs if key in self._ATTRIBUTES},
                'path': path, 'children': {}, 'text': [], 'skip': skipped, 'private_input': private_input}
        if not skipped and (tag.startswith('wx-') or tag in ('input', 'textarea', 'button', 'a')):
            self.nodes.append(node)
        if tag not in self._VOID:
            self.stack.append(node)
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)
    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                self.stack[index:] = []
                return
    def handle_data(self, data):
        if (self.stack and not self.stack[-1]['skip'] and
                not self.stack[-1]['private_input'] and data.strip()):
            for node in self.stack:
                node['text'].append(data)
    def components(self):
        result = []
        for node in self.nodes:
            attrs = node['attrs']
            text = ' '.join(''.join(node['text']).split())
            name = (attrs.get('aria-label') or attrs.get('name') or attrs.get('id') or
                    text or attrs.get('placeholder') or node['tag'])
            result.append(MiniProgramComponent(node['tag'], name, text, attrs.get('id', ''),
                attrs.get('class', ''), attrs.get('role', ''), attrs.get('placeholder', ''),
                'disabled' in attrs or attrs.get('aria-disabled') == 'true', node['path'], self.page))
        return result


def parse_components(html, page):
    """只保留组件描述，不保留图片 URL、输入值、内联脚本或隐藏数据属性。"""
    if not isinstance(html, str) or not isinstance(page, MiniProgramPage):
        raise ValueError('小程序组件数据无效')
    parser = _ComponentParser(page)
    parser.feed(html)
    parser.close()
    return parser.components()


def _document_data(value):
    try:
        result = json.loads(value['cdp_response'])
        if not isinstance(result, dict) or 'error' in result or not isinstance(result.get('result'), dict):
            raise ValueError()
        return result['result']
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError('小程序组件树读取失败') from None


def _page_document(root, page_id):
    """CDP 外层树只返回 iframe 元信息，继续读取当前页的 document 节点。"""
    if not isinstance(root, dict):
        raise RuntimeError('小程序组件树结构无效')
    target = 'page-frame_' + str(page_id)
    pending = [root]
    while pending:
        node = pending.pop()
        attrs = node.get('attributes', [])
        attrs = dict(zip(attrs[::2], attrs[1::2]))
        if attrs.get('name') == target:
            document = node.get('contentDocument', {})
            if type(document.get('nodeId')) is int:
                return document['nodeId']
        pending.extend(node.get('children', []))
    raise RuntimeError('当前小程序页面尚未加载组件树，请稍后重试')


class MiniProgramClient:
    """通过 ServiceFacade.miniprogram(app_id) 获取，沿用当前机器人账号。

    click 坐标使用原生 screenshot 的页面坐标，不使用桌面坐标或系统鼠标。
    input_text 向当前小程序内已聚焦的输入框写入，不主动提交表单。
    """
    def __init__(self, app_id, command):
        if not isinstance(app_id, str) or not re.fullmatch(r'wx[0-9a-f]{16}', app_id):
            raise ValueError('小程序 AppID 应为 wx 开头的 18 位标识')
        if not callable(command):
            raise TypeError('需要小程序原生命令通道')
        self.app_id, self._command = app_id, command
        self._session = 'helper_mp_' + uuid.uuid4().hex
        self._opened = False
        self._lock = threading.RLock()

    def _call(self, requests, *, opening=False):
        if not opening and not self._opened:
            raise RuntimeError('请先调用 open() 打开目标小程序')
        return self._command(self.app_id, self._session, requests, check_app=not opening)

    @staticmethod
    def _request(method, **params):
        return {'method': method, 'params': params}

    def open(self, entry_url=''):
        """原生唤起；不扫描二维码，不授予小程序权限。"""
        if not isinstance(entry_url, str) or '\0' in entry_url:
            raise ValueError('小程序页面路径无效')
        with self._lock:
            params = {'app_id': self.app_id, 'prefer_hot_launch': True}
            if entry_url:
                params['entry_url'] = entry_url
            result = self._call([self._request('XWeb.LaunchApplet', **params)], opening=True)[0]
            if result.get('app_id') != self.app_id:
                raise RuntimeError('原生接口尚未确认目标小程序已打开')
            self._opened = True
            return result

    def page_info(self):
        with self._lock:
            return self._call([self._request('XWeb.GetAppletPageInfo')])[0]

    def evaluate(self, script):
        """在小程序 AppService 执行 JS；这里没有浏览器 window/document。"""
        if not isinstance(script, str) or not script.strip() or '\0' in script:
            raise ValueError('小程序脚本不能为空或包含空字符')
        with self._lock:
            data = self._call([self._request('XWeb.EvaluateJavaScript', script=script, timeout=5000)])[0]
            result = data.get('result', {})
            if data.get('error') or not isinstance(result, dict) or result.get('success') is not True:
                raise RuntimeError('小程序脚本执行失败')
            return result.get('data')

    def _page(self):
        data = self.evaluate("(()=>{const p=getCurrentPages().slice(-1)[0];"
                             "return p?{route:p.route,page_id:p.__wxWebviewId__}:null})()")
        if (not isinstance(data, dict) or type(data.get('page_id')) is not int or
                not isinstance(data.get('route'), str)):
            raise RuntimeError('小程序当前页面尚未就绪')
        return MiniProgramPage(self.app_id, data['route'].split('?')[0], data['page_id'], self._session)

    def components(self):
        """读取当前页面的组件类型、名称、文字、ID、类名及所在页面。"""
        with self._lock:
            page = self._page()
            data = self._call([self._request('DOM.getDocument')])[0]
            document = _page_document(_document_data(data).get('root'), page.page_id)
            data = self._call([self._request('DOM.getOuterHTML', nodeId=document)])[0]
            html = _document_data(data).get('outerHTML')
            if not isinstance(html, str):
                raise RuntimeError('小程序组件内容尚未就绪')
            if page != self._page():
                raise RuntimeError('读取期间小程序页面已切换，请重新获取组件')
            return parse_components(html, page)

    def click(self, x, y, *, expected_page=None):
        """向小程序引擎派发一次点击，坐标与原生截图一致；不移动鼠标。"""
        if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 0x7fffffff
               for value in (x, y)):
            raise ValueError('点击坐标需要非负有限数值')
        with self._lock:
            if expected_page is not None and expected_page != self._page():
                raise RuntimeError('组件所属页面已切换，请重新定位')
            self._call([self._request('Input.dispatchTouchEvent', type='touchStart',
                          touchPoints=[{'x': x, 'y': y, 'id': 1}]),
                        self._request('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])])
            return {'status': 'dispatched'}

    def input_text(self, text, *, replace=False, expected_page=None):
        """输入到已聚焦的组件；replace=True 先在小程序内部全选替换。"""
        if not isinstance(text, str) or '\0' in text or type(replace) is not bool:
            raise ValueError('输入文字或替换选项无效')
        text.encode('utf-8')
        with self._lock:
            if expected_page is not None and expected_page != self._page():
                raise RuntimeError('输入框所属页面已切换，请重新定位')
            requests = []
            if replace:
                for kind in ('rawKeyDown', 'keyUp'):
                    requests.append(self._request('Input.dispatchKeyEvent', type=kind, key='a',
                        code='KeyA', windowsVirtualKeyCode=65, modifiers=2))
            requests.append(self._request('Input.insertText', text=text))
            self._call(requests)
            return {'status': 'dispatched'}

    def screenshot(self):
        """返回原生 JPEG 字节，由调用者选择是否保存。"""
        with self._lock:
            data = self._call([self._request('Page.captureScreenshot')])[0]
            try:
                image = base64.b64decode(data['data'], validate=True)
                if not image.startswith(b'\xff\xd8\xff'):
                    raise ValueError()
                return image
            except (ValueError, TypeError, KeyError):
                raise RuntimeError('小程序截图无效') from None

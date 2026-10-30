"""管理台 HTTP 传输：本机访问校验、静态资源和 API 路由。"""
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

from .panel_common import PanelError, redact


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, controller, port=8765):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), PanelHandler)
        self.timeout = 0.5
        port = self.server_address[1]
        self.origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
        self.hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}


class PanelHandler(BaseHTTPRequestHandler):
    server_version = 'helper-local'

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send(self, status, data, kind='application/json; charset=utf-8'):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8') if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _check(self, *, api=False):
        if self.headers.get('Host') not in self.server.hosts:
            raise PanelError('不允许的访问地址', 403)
        origin = self.headers.get('Origin')
        if origin is not None and origin not in self.server.origins:
            raise PanelError('不允许跨站访问管理台', 403)
        if api and not secrets.compare_digest(self.headers.get('X-Helper-Token', ''), self.server.token):
            raise PanelError('页面会话已失效，请刷新页面', 403)

    def _handle(self, write=False):
        try:
            self._check(api=self.path.startswith('/api/'))
            parsed = urlsplit(self.path)
            path = parsed.path
            controller = self.server.controller
            if not write and path in ('/', '/index.html', '/style.css', '/app.js', '/moments.js'):
                name = 'index.html' if path == '/' else path[1:]
                raw = (controller.root / 'webui' / name).read_bytes()
                if name == 'index.html':
                    raw = raw.replace(b'__HELPER_TOKEN__', html.escape(self.server.token).encode('ascii'))
                kind = {'html': 'text/html', 'css': 'text/css', 'js': 'application/javascript'}[name.rsplit('.', 1)[1]]
                return self.send(200, raw, kind + '; charset=utf-8')
            hub = controller if hasattr(controller, 'robot') else None
            global_routes = ('/api/robots', '/api/accounts', '/api/accounts/prepare', '/api/accounts/start',
                             '/api/wechat-login/start', '/api/wechat-login/attach',
                             '/api/wechat-login/status', '/api/wechat-login/cancel')
            if hub is not None and path not in global_routes:
                robot_id = parse_qs(parsed.query).get('robot', ['default'])[0]
                controller = hub.robot(robot_id)
            if write:
                if (self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding') or
                        not self.headers.get('Content-Length', '').isdigit()):
                    raise PanelError('请使用 JSON 请求')
                size = int(self.headers['Content-Length'])
                limit = 2 * 1024 * 1024 if path in ('/api/plugin-source', '/api/plugin-reset', '/api/plugin-business') else 65536
                if path == '/api/moments-publish':
                    limit = 29 * 1024 * 1024
                # Text is split downstream; its size is constrained by available
                # resources instead of a separate fixed message-size limit.
                if size < 1 or (path != '/api/send-text' and size > limit):
                    # Windows 关闭仍有未读请求体的连接时可能发送 RST，掩盖 413 响应。
                    # 只丢弃略超限的有界请求，限制等待，绝不解析或保存它。
                    if 0 < size - limit <= 65536:
                        previous_timeout = self.connection.gettimeout()
                        try:
                            self.connection.settimeout(2)
                            self.rfile.read(size)
                        except (OSError, TimeoutError):
                            pass
                        finally:
                            self.connection.settimeout(previous_timeout)
                    raise PanelError('请求大小超出限制', 413)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise PanelError('请求必须为 JSON 对象')
                if path == '/api/robots' and hub is not None:
                    return self.send(200, hub.create_robot(data))
                if path == '/api/accounts/prepare' and hub is not None:
                    return self.send(200, hub.prepare_account(data))
                if path == '/api/accounts/start' and hub is not None:
                    return self.send(200, hub.start_selected(data))
                if path == '/api/wechat-login/start' and hub is not None:
                    return self.send(200, hub.wechat_login().start(data))
                if path == '/api/wechat-login/attach' and hub is not None:
                    return self.send(200, hub.wechat_login().attach(data))
                if path == '/api/wechat-login/cancel' and hub is not None:
                    return self.send(200, hub.wechat_login().cancel(data))
                if path == '/api/send-text':
                    return self.send(200, controller.send_text(data))
                if path == '/api/scheduled-tasks':
                    return self.send(200, controller.save_scheduled_task(data))
                if path == '/api/scheduled-tasks/delete':
                    return self.send(200, controller.delete_scheduled_task(data))
                if path == '/api/scheduled-tasks/toggle':
                    return self.send(200, controller.toggle_scheduled_task(data))
                if path == '/api/moments-publish':
                    return self.send(200, controller.publish_moments(data))
                if path == '/api/moments-read':
                    return self.send(200, controller.read_moments(data))
                if path == '/api/moments-interact':
                    return self.send(200, controller.interact_moments(data))
                if path in ('/api/plugin-source', '/api/plugin-reset'):
                    return self.send(200, controller.save_plugin_source(data, reset=path == '/api/plugin-reset'))
                if path == '/api/plugin-business':
                    return self.send(200, controller.save_plugin_business(data))
                actions = {'/api/start': lambda: controller.start(), '/api/stop': lambda: controller.stop(),
                    '/api/restart': lambda: controller.restart(),
                    '/api/account': lambda: controller.bind_account(data),
                    '/api/config': lambda: controller.save_configuration(data),
                    '/api/plugin': lambda: controller.plugin_action(data)}
                if path not in actions:
                    raise PanelError('接口不存在', 404)
                actions[path]()
                return self.send(200, {'ok': True})
            if path == '/api/state':
                return self.send(200, controller.snapshot())
            if path == '/api/scheduled-tasks':
                return self.send(200, controller.list_scheduled_tasks())
            if path == '/api/scheduled-actions':
                return self.send(200, controller.scheduled_action_types())
            if path == '/api/config':
                return self.send(200, controller.configuration())
            if path == '/api/plugin-source':
                return self.send(200, controller.plugin_editor(parse_qs(parsed.query).get('name', [''])[0]))
            if path == '/api/plugin-business':
                return self.send(200, controller.plugin_business(parse_qs(parsed.query).get('name', [''])[0]))
            if path == '/api/accounts':
                return self.send(200, hub.logged_accounts() if hub is not None else {'accounts': controller.accounts()})
            if path == '/api/wechat-login/status' and hub is not None:
                return self.send(200, hub.wechat_login().status(parse_qs(parsed.query).get('attempt_id', [''])[0]))
            if path == '/api/chats':
                return self.send(200, {'chats': controller.chats()})
            if path == '/api/group-members':
                return self.send(200, controller.group_members(parse_qs(parsed.query).get('room', [''])[0]))
            if path == '/api/contacts':
                return self.send(200, controller.contacts())
            if path == '/api/send-connection':
                return self.send(200, controller.send_connection())
            if path == '/api/send-status':
                return self.send(200, controller.send_status(parse_qs(parsed.query).get('id', [''])[0]))
            if path == '/api/moments-connection':
                return self.send(200, controller.moments_connection())
            if path == '/api/moments-status':
                return self.send(200, controller.moments_status(parse_qs(parsed.query).get('id', [''])[0]))
            if path == '/api/moments-interaction-status':
                return self.send(200, controller.moments_interaction_status(parse_qs(parsed.query).get('id', [''])[0]))
            if path == '/api/logs':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                return self.send(200, controller.logs.tail(max(0, after)))
            if path == '/api/messages':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                return self.send(200, controller.message_tail(after))
            if path == '/api/robots':
                return self.send(200, hub.robot_list() if hub is not None else
                    {'robots': [{'id': 'default', 'name': '默认机器人', 'account': '', 'state': controller.state}]})
            raise PanelError('页面不存在', 404)
        except PanelError as error:
            self.send(error.status, {'error': redact(error)})
        except (ValueError, TypeError, KeyError) as error:
            self.send(400, {'error': redact(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:
            logging.getLogger('WebPanel').exception('管理操作失败')
            self.send(500, {'error': redact(error)})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(write=True)

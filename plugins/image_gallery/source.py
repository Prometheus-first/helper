"""固定图库请求、受限图片下载与单次文件租约；不接收聊天正文。"""
from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_JSON_BYTES = 64 * 1024
MAX_PIXELS = 20_000_000
PRESETS = {
    'xxapi': ('https://v2.xxapi.cn/api/meinvpic', 'data', ('images.xxapi.cn', 'cdn.api-m.com')),
    'wzapi': ('https://wzapi.com/api/sjmntp?format=json', 'data.wallpaper', ('toutiaoimg.com',)),
}


class GalleryError(RuntimeError):
    """只包含可公开的错误说明，不附带原始 URL、响应或密钥。"""


class GalleryCancelled(GalleryError):
    pass


def finite_number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'image_gallery.{name} 应为 {low}–{high} 的有限数值')
    return value


def _hostname(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', value)
            or value.startswith('.') or value.endswith('.')):
        raise ValueError('image_gallery.image_hosts 应填写完整域名或域名后缀')
    value = value.lower()
    if value == 'localhost' or value.endswith(('.localhost', '.local')):
        raise ValueError('图库域名不能使用本地地址')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if '.' not in value or any(not part or len(part)>63 for part in value.split('.')):
            raise ValueError('图库域名格式无效') from None
    else:
        if not address.is_global:
            raise ValueError('图库域名不能使用内网地址')
    return value


def checked_url(value, hosts=None):
    if (not isinstance(value, str) or not value or len(value)>4096 or '\\' in value
            or any(ord(char)<=32 or ord(char)==127 for char in value)):
        raise GalleryError('图库图片地址无效')
    try:
        parsed=urllib.parse.urlsplit(value)
        host=_hostname(parsed.hostname or '')
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        if parsed.port is not None and not 1<=parsed.port<=65535:
            raise ValueError()
        if hosts is not None and not any(host==allowed or host.endswith('.'+allowed) for allowed in hosts):
            raise ValueError()
    except ValueError:
        raise GalleryError('图库图片地址不在允许范围') from None
    return value


def _origin(url):
    value=urllib.parse.urlsplit(url)
    return value.scheme, value.hostname, value.port or 443


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ImageLease:
    def __init__(self, path):
        self.path=path
        self.retained=False

    def retain(self):
        # 原生任务可能仍在排队；未确认结果时不能删除它尚可能读取的源文件。
        self.retained=True

    def close(self):
        if not self.retained:
            self.path.unlink(missing_ok=True)
            self.path.parent.rmdir()


class GalleryClient:
    def __init__(self, directory, options):
        self.directory=Path(directory).resolve()
        self.source=options.get('source','xxapi')
        if self.source not in (*PRESETS,'custom'):
            raise ValueError('image_gallery.source 应为 xxapi、wzapi 或 custom')
        if self.source=='custom':
            self.endpoint=options.get('api_url','')
            self.json_path=options.get('json_path','data')
            preset_hosts=()
        else:
            self.endpoint,self.json_path,preset_hosts=PRESETS[self.source]
        checked_url(self.endpoint)
        self.response_type=options.get('response_type','json') if self.source=='custom' else 'json'
        if self.response_type not in ('json','image'):
            raise ValueError('image_gallery.response_type 应为 json 或 image')
        if (not isinstance(self.json_path,str) or not self.json_path
                or len(self.json_path)>256 or len(self.json_path.split('.'))>16):
            raise ValueError('image_gallery.json_path 应为图片地址的点分字段路径')
        hosts=options.get('image_hosts',list(preset_hosts))
        if not isinstance(hosts,list) or not all(isinstance(host,str) for host in hosts) or len(hosts)>16:
            raise ValueError('image_gallery.image_hosts 应为最多 16 个域名的列表')
        self.hosts=frozenset([_hostname(urllib.parse.urlsplit(self.endpoint).hostname),*(_hostname(host) for host in hosts)])
        self.timeout=finite_number(options.get('timeout_seconds',12),'timeout_seconds',1,30)
        self.max_bytes=options.get('max_image_bytes',10*1024*1024)
        if type(self.max_bytes) is not int or not 1024<=self.max_bytes<=20*1024*1024:
            raise ValueError('image_gallery.max_image_bytes 应为 1024–20971520 的整数')
        self.headers=options.get('headers',{})
        if not isinstance(self.headers,dict) or len(self.headers)>16:
            raise ValueError('image_gallery.headers 应为请求头对象')
        for key,value in self.headers.items():
            if (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9!#$%&\'*+.^_`|~-]{1,64}',key)
                    or key.lower() in ('host','content-length','transfer-encoding','connection','proxy-authorization')
                    or not isinstance(value,str) or len(value)>4096 or any(char in value for char in '\r\n\0')):
                raise ValueError('image_gallery.headers 包含无效请求头')
        self.headers=dict(self.headers)
        self.opener=urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _check(current):
        if not current():
            raise GalleryCancelled('图库请求已取消')

    def _get(self, url, *, image, current, api_request=False):
        limit=self.max_bytes if image else MAX_JSON_BYTES
        initial=_origin(self.endpoint)
        deadline=time.monotonic()+self.timeout
        for _ in range(4):
            self._check(current)
            checked_url(url,self.hosts)
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise GalleryError('图库请求超时')
            headers={'User-Agent':'helper-image-gallery','Accept':'image/*' if image else 'application/json',
                     'Accept-Encoding':'identity'}
            if api_request and _origin(url)==initial:
                headers.update(self.headers)
            request=urllib.request.Request(url,headers=headers)
            try:
                with self.opener.open(request,timeout=remaining) as response:
                    length=response.headers.get('Content-Length')
                    if length is not None and (not length.isdecimal() or int(length)>limit):
                        raise GalleryError('图库响应超过大小限制')
                    if response.headers.get('Content-Encoding','identity').lower() not in ('','identity'):
                        raise GalleryError('图库响应编码不受支持')
                    blocks=[];total=0
                    read=getattr(response,'read1',response.read)
                    while True:
                        self._check(current)
                        if time.monotonic()>deadline:
                            raise GalleryError('图库请求超时')
                        chunk=read(min(64*1024,limit-total+1))
                        if not chunk:
                            break
                        blocks.append(chunk);total+=len(chunk)
                        if total>limit:
                            raise GalleryError('图库响应超过大小限制')
                    if length is not None and total!=int(length):
                        raise GalleryError('图库响应不完整')
                    return b''.join(blocks),url
            except urllib.error.HTTPError as error:
                code=error.code;location=error.headers.get('Location') if error.headers else None
                error.close()
                if code in (301,302,303,307,308) and location:
                    url=urllib.parse.urljoin(url,location)
                    continue
                raise GalleryError(f'图库请求失败（HTTP {code}）') from None
            except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException):
                raise GalleryError('图库连接失败或超时') from None
        raise GalleryError('图库重定向次数过多')

    def _image(self, raw):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as check:
                    if check.format not in ('JPEG','PNG','WEBP','GIF','BMP') or not 0<check.width*check.height<=MAX_PIXELS:
                        raise GalleryError('图库图片格式或尺寸不受支持')
                    check.verify()
                with Image.open(BytesIO(raw)) as source:
                    source.seek(0)
                    upright=ImageOps.exif_transpose(source).convert('RGBA')
                    clean=Image.new('RGB',upright.size,'white')
                    clean.paste(upright,(0,0),upright.getchannel('A'))
                    output=BytesIO();clean.save(output,format='JPEG',quality=92)
            value=output.getvalue()
            if len(value)>20*1024*1024:
                raise GalleryError('图库图片超过发送大小限制')
            return value
        except (OSError,ValueError,SyntaxError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning):
            raise GalleryError('图库未返回有效的完整图片') from None

    @contextmanager
    def fetch(self, *, current=lambda:True):
        raw,final=self._get(self.endpoint,image=self.response_type=='image',current=current,api_request=True)
        if self.response_type=='json':
            try:
                result=json.loads(raw)
                if self.source=='xxapi' and (not isinstance(result,dict) or type(result.get('code')) is not int or result['code']!=200):
                    raise ValueError()
                if self.source=='wzapi' and (not isinstance(result,dict) or result.get('status')!='success'):
                    raise ValueError()
                value=result
                for part in self.json_path.split('.'):
                    value=value[int(part)] if isinstance(value,list) and part.isdecimal() else value[part]
                if not isinstance(value,str):raise ValueError()
                url=urllib.parse.urljoin(final,value)
            except (ValueError,TypeError,KeyError,IndexError,RecursionError):
                raise GalleryError('图库未返回可用的图片地址') from None
            raw,_=self._get(url,image=True,current=current)
        self._check(current)
        image=self._image(raw)
        self._check(current)
        folder=self.directory/('request-'+uuid.uuid4().hex)
        folder.mkdir(parents=True)
        lease=ImageLease(folder/'image.jpg')
        try:
            lease.path.write_bytes(image)
            yield lease
        finally:
            lease.close()

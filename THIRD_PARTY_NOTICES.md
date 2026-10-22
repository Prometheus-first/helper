# 第三方组件

根目录 `LICENSE` 的非商业使用条款仅适用于本项目著作权人有权授权的原创部分，不替代、修改或缩减下列第三方组件及依赖各自许可证授予的权利。原始版权声明、授权条件及免责声明继续适用。

原生桥接使用 MinHook 1.3.4，并保留构建所需的源码、头文件、作者信息及原始许可证：

- `native/vendor/minhook-1.3.4/LICENSE.txt`
- `native/vendor/minhook-1.3.4/AUTHORS.txt`
- `native/vendor/minhook-1.3.4/src/hde/` 下的原始版权声明

保留的 DLL 由本项目桥接源码和上述组件构建；不包含微信客户端程序或微信核心 DLL。

Python 依赖通过 `requirements.txt` 安装，不将它们的二进制复制进本仓库。表情缓存解密使用 cryptography，图片结构校验使用 Pillow，WXGF 动画转换使用 imageio-ffmpeg 所带的 FFmpeg；这些软件的许可证随各自发行包提供。

可选的 WAV 语音转码依赖通过 `requirements-voice.txt` 安装，使用 [silk-python / pysilk](https://github.com/synodriver/pysilk)；其 Python 绑定与 SILK 编码器的许可证随发行包提供。本仓库不复制编码器源码或二进制。

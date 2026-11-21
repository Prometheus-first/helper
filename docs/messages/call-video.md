# 视频通话画面替换（实验功能）

Windows `native_text` 后端可以把已经发起的视频通话画面替换为本地视频，或程序提交的 I420 帧。插件声明 `"requires": ["call_video.replace"]`。支持已核对的微信 **4.2.0.11 / 4.2.0.12 x64**：两版的 `VoipEngine.dll` 文件指纹完全相同，账号布局分别按核心 DLL 版本解析。核心 DLL 与视频引擎仍分别按完整指纹校验；其他构建会拒绝启用。此功能不负责发起、接听通话，也不替换通话音频。

当前实测范围为一对一视频通话；群视频通话尚未验证。

## 在网页控制台使用换脸

素材库同时支持图片和视频。点击图片使用该人脸；点击视频按原速播放一次，播放完成后自动恢复到之前的换脸图片。播放中点击另一段视频会重新开始播放新视频；点击图片则打断视频并切换人脸。视频解码和换脸复用同一个帧源，换脸模型在播放期间保留，不在结束时重新加载。

点击“添加素材”上传 MP4、MOV、WebM 或 MKV，单个视频最多 128 MiB、15 分钟。上传分块传输，可以取消；重复上传同一视频会合并。视频和图片按当前账号保存在同一私有素材库中，合计最多 24 个条目。视频条目显示时长和播放标记，播放时显示进度。首次直接点击视频时，需要素材库中已有一张换脸图片，作为播放结束后的返回画面。

这里只替换视频画面，视频文件中的声音不发送到通话，通话音频继续沿用微信。视频读取失败时回到换脸，停止或账号切换时结束当前播放；不会将视频任务带入新连接。

运行 `start_web.bat`，在运行概览启动要使用的微信账号，再打开左侧导航最下方的“视频换脸”（位于“运行日志”之后）。点击“添加素材”选择本机照片，选择“均衡 · 256 模型”或“流畅 · 128 模型”，然后点击“开始换脸”，在微信中发起或接听视频通话。首次加载模型需要稍候；尚未接通时页面显示等待通话。管理台默认仍进入运行概览。

照片会保存在当前账号 workspace 的 `.runtime/call-video/` 私有目录，重启控制台后仍可使用；其他账号有独立照片库。支持静态 PNG、JPEG、WebP，每张不超过 8 MiB、2000 万像素，每个账号最多 24 张。上传时转正方向、移除元数据并转换为本地 JPEG；重复添加相同照片会复用已保存的条目。源码和 Git 不包含这些照片。

运行中直接点击照片即可切换；模型就绪后添加新照片也会自动切换。切换复用已有工作进程和 GPU 模型；新照片检查失败时保留原照片。页面在模型确认后更新“正在使用”，不把点击操作本身当作切换成功。正在使用或待切换的照片须先停止换脸或切换到其他照片才能移除。

本机预览通过受管理台令牌保护的本地接口读取，摄像头帧仅在内存中传递。点击“停止换脸”、挂断或账号连接结束后会停止当前视频源，切换账号不会自动把上一账号的换脸任务带过去。网页入口没有命令行测试脚本的约 20 分钟限制。

首次使用可展开“运行环境设置”，核对已安装的 FaceFusion Python 和源码目录；设置保存在本机运行目录。也可通过 `WXBOT_FACEFUSION_PYTHON`、`WXBOT_FACEFUSION_REPOSITORY` 提供初始默认路径。依赖安装及固定版本见下方 FaceFusion 章节。

## 播放本地文件

先连接框架，启用只读统计，再手动发起测试视频通话：

```python
ctx.call_video_statistics(arm=True)
```

通话发送帧开始后，读取实际采集尺寸。`input_width`、`input_height` 为零时尚未观察到发送帧；不要猜测尺寸。以下调用由插件自己的控制入口执行：

```python
stats = ctx.call_video_statistics()
width, height = stats['input_width'], stats['input_height']
if width and height:
    ctx.start_call_video_file(
        r'E:\videos\sample.mp4', width=width, height=height,
        fps=15, loop=True, quality='high',
    )

# 停止替换，恢复微信原有摄像头路径
ctx.stop_call_video()
```

文件在独立工作线程解码，等比例缩放、黑边补齐并转换为 I420。首次使用需安装 `imageio-ffmpeg`，或让系统 PATH 中的 FFmpeg 可用。支持偶数尺寸，边长 16–1920、总像素不超过 2073600；帧率 1–30。`loop=False` 时播放结束后恢复原摄像头路径。若采集尺寸改变，替换帧会被拒绝；停止后按新的统计尺寸重新开启视频源。

## 接入程序画面

文件播放和程序帧共用同一条原生通道。停止文件源后，可以逐帧提交程序生成的完整、紧密排列 I420 数据：先 Y，再 U，再 V，字节数为 `width * height * 3 // 2`。

```python
ctx.start_call_video_frames(width, height, quality='high')
# 在自己的渲染循环中调用；不要与文件播放同时写入。
accepted = ctx.submit_call_video_frame(i420_bytes)
ctx.stop_call_video()
```

`accepted=False` 表示三个槽位都在使用，当前帧被丢弃；调用方继续生成下一帧即可。接口不维护视频积压队列。跨消息或异步控制应传入 `expected_session=message.session_id`，避免旧连接的任务控制新账号。

## 清晰度与验证范围

`quality='native'` 保留微信的尺寸和码率选择；`quality='high'` 在自定义帧调用期间提示使用经实测的 640 档。对已核对的软件编码后端，将低于 1.2 Mbps 的目标码率临时提高到 1.2 Mbps；其他后端保持微信原有设置。尺寸选项和码率字段都在每次原生调用结束后恢复。若编码器拒绝高清帧，后续帧自动退回原生设置，`stats['encoder']['fallback']` 为 `1`。`stats['encoder']` 显示编码前实际尺寸、本次目标码率、帧率上限和原生返回值；目标码率不等于实际网络吞吐，也不是接收端画质评分。

真实双端测试已确认本地 MP4 能连续播放、方向正常。该次采集为 720×1280 I420、约 15 fps：原生选择的编码输入为 320×144、目标码率 300 kbps；初次 640 档测试实际为 640×288，同样目标码率，接收端确认明显更清楚。后续换脸测试中，本机画面清楚但对端模糊，临时提高编码目标到 1.2 Mbps 后用户确认该版本可用，随后接入原生调用的作用域恢复逻辑。1280 档在该编码器上被拒绝，因此没有开放该档位，也没有宣称支持 720p/1080p 高清通话。

原生桥接只消费已经准备好的帧，不在回调中解码、读文件或等待 Python。共享内存固定三个槽位；写入与读取使用原子占用状态，读取期间不会覆写。过期 500 ms、源退出、文件结束、停止、账号变化或登录上下文变化时恢复原帧。停止服务会先终止解码器，再解除帧通道、等待回调退出并卸载桥接。诊断仅返回尺寸、格式与计数，不保存摄像头画面。

## FaceFusion 实时摄像头换脸（可选）

集成固定版本 [FaceFusion 3.9.2](https://github.com/facefusion/facefusion/releases/tag/3.9.2)，提交为 `decc6c9d8f8e014878624452c6e8fe208726b4ec`。选型主要看可复现的源码版本、Windows/CUDA 支持和逐帧处理能力；不把 GitHub 星数当作稳定性保证。对比的 [DeepFaceLive](https://github.com/iperov/DeepFaceLive) 已归档，[Deep-Live-Cam](https://github.com/hacksider/Deep-Live-Cam) 的官网新发行版与公共源码版本不同，本次未集成这两者。

先安装 Git，并确保启动管理台的环境能找到 `git`。工作进程也会检查 Git for Windows 的安装记录和常见目录；便携安装可通过 `WXBOT_GIT_EXECUTABLE` 指定 `git.exe` 的完整路径，然后重启管理台。

为 FaceFusion 单独建立 Python 3.12 环境，不要把 GPU 依赖装入机器人环境。下列命令在项目目录执行；环境和第三方源码放在项目外：

```cmd
conda create --prefix E:\softwares\facefusion-env python=3.12 pip=25.0 -y
git clone --depth 1 --branch 3.9.2 https://github.com/facefusion/facefusion.git E:\softwares\facefusion
E:\softwares\facefusion-env\python.exe -I -m pip install -r requirements-facefusion.txt
```

`-I` 禁用用户级 Python 包和环境路径注入。工作进程会在自身环境内加载 CUDA/cuDNN DLL，校验上游内容检查模块和模型哈希，预热实际换脸模型，并拒绝静默退回 CPU。首次使用会从上游配置的模型源下载模型；照片和摄像头帧由本机工作进程处理，不发给模型下载服务器。

插件沿用 `call_video.replace` 能力和连接校验，在已经观察到视频采集尺寸后启动：

```python
stats = ctx.call_video_statistics(arm=True)
if stats['input_width'] and stats['input_height']:
    ctx.start_call_video_facefusion(
        r'E:\photos\source.png',
        python_executable=r'E:\softwares\facefusion-env\python.exe',
        repository=r'E:\softwares\facefusion',
        width=stats['input_width'], height=stats['input_height'],
        fps=15, quality='high',
        model='hyperswap_1a_256', enhance=False,
        expected_session=message.session_id,
    )

# 查看异步模型加载、帧数、延迟和错误状态
stats = ctx.call_video_statistics()
ai = stats['ai']
ctx.stop_call_video()
```

`model='inswapper_128_fp16'`、`enhance=False` 是偏速度的默认设置。`hyperswap_1a_256`、`hyperswap_1b_256`、`hyperswap_1c_256` 可切换到 256 像素模型；`enhance=True` 增加 GPEN 256 面部增强。画质和帧率需在实际人脸、光照及显卡上比较。替换主要作用于面部，源照片的发型、头型和环境光不会完整移植；单张照片也不保证侧脸、遮挡和大表情都自然。

实时推理的工作画面长边限制为 640 像素，再缩放回微信实际采集尺寸，减少大分辨率的逐帧遮罩及融合开销；输出尺寸不代表换脸模型细节。通话卡顿时优先使用默认快速模型并关闭增强。可比较一段时间内 `ai['frames']` 的增量来计算实际输出帧率，不能用原生发送回调数代替推理帧率。

本机 RTX 4060 Laptop 的真实通话测试中，接收端已确认换脸画面、转头及眨眼可正常传输。256 模型加增强曾出现明显卡顿；切换默认 128 模型、关闭增强并限制工作分辨率后，接收端确认流畅。约 20 秒混合检测样本的输出约 8.5 fps，平均推理约 109 ms、采集到框架接收约 159 ms，无过期帧丢弃；这不包含网络及对端解码延迟，也不是其他设备的性能保证。

微信通话模式直接复用微信已采集的 I420 原始帧，推理工作进程不再打开第二个摄像头，避免同时采集导致的花屏和设备冲突。原生回调在替换前把原始帧复制到独立的三槽共享内存，工作进程只读取最新完整帧，输出再复用原有 I420 发送通道；不会把上一次换脸输出再次用作输入。输入映射沿用账号、登录上下文、随机代号和关闭状态校验，不保存原始像素。`camera_id` 仅用于独立本机摄像头预览，通话模式使用微信选定的设备；`ai['input_source'] == 'wechat'` 可确认输入路径。

推理在独立进程运行，静态人脸缓存每帧清理后仅保留固定源照片。输出帧通过有界二进制协议返回，再复用 I420 通道送给原生桥接。Windows Job Object 保证机器人异常退出时结束 AI 子进程。采集和接收都使用 `GetTickCount64`，避免不同 Python 版本的单调时钟基准差异造成误判。`ai['faces']`、`ai['swapped_frames']` 用于区分摄像头有输出和实际检测到人脸的帧；不会返回人脸特征或身份推断。

本机预览是带随机路径的临时回环 HTTP 页面，图片只在内存中传递，始终带 `AI FACE SWAP TEST` 标记。无可检测人脸时上游会返回原摄像头画面；停止、出错、超时或切换账号时，微信也恢复原摄像头路径。需要始终隐藏原始画面的场景应先做独立的遮挡策略，不能依赖本功能隐藏身份。权重许可与代码许可分别遵循上游说明。


## 照片整体外观驱动（FasterLivePortrait）

`start_call_video_liveportrait` 保留照片整体构图，用摄像头提取头部和表情变化。固定上游 [FasterLivePortrait v2.0](https://github.com/warmshao/FasterLivePortrait/tree/548b5ccc7d92abfaaaeceba7ff1828d657856ec5)，源码提交 `548b5ccc7d92abfaaaeceba7ff1828d657856ec5`，人像 ONNX 权重来自作者的 [Hugging Face 仓库](https://huggingface.co/warmshao/FasterLivePortrait/tree/eb937f4bec7186598df2d1f68e1ddbb488ae1de5/liveportrait_onnx)。源码、权重和运行环境放在项目外，不提交照片或摄像头帧。

使用单独的 Python 3.12 环境，安装 `requirements-liveportrait.txt`。PyTorch 版本固定 `2.9.1+cu128`，需从官方 `https://download.pytorch.org/whl/cu128` 索引安装；InsightFace 0.7.3 在 Windows 上需要 MSVC 和 Windows SDK 编译。首次运行仅从模型目录读取人像模型，不自动下载动物模型或音频驱动模型。

```python
stats = ctx.call_video_statistics(arm=True)
if stats['input_width'] and stats['input_height']:
    ctx.start_call_video_liveportrait(
        r'E:\photos\source.png',
        python_executable=r'E:\softwares\liveportrait-env\python.exe',
        repository=r'E:\softwares\FasterLivePortrait',
        width=stats['input_width'], height=stats['input_height'],
        camera_id=0, fps=15, quality='high',
        expected_session=message.session_id,
    )
ctx.stop_call_video()
```

复用 FaceFusion 的有界进程协议、Job Object、I420 发布通道和会话生命周期。推理、摄像头和依赖均在隔离工作进程中。旧模型的两个五维 `GridSample` 在当前 ONNX Runtime 上无法直接进行 CUDA 推理：适配层按图边界拆分模型，用 CUDA I/O binding 保持中间张量在显存，两个采样操作调用 PyTorch CUDA。默认半精度，生成的分段模型放在外部权重目录，缓存按原始模型和生成文件 SHA-256 校验；不修改上游源码和原始权重。

摄像头先做人脸裁剪再提取动作，采用相对姿态、平滑和动作限幅，并固定位置、缩放以减少头发和耳饰拖影。预览页可在正面对镜头、自然表情时重新校准。无可检测人脸时显示静态源照片，不显示摄像头原图。输出保留整张照片并添加 `AI PORTRAIT TEST` 标记。

这是单张照片的二维人像动画，不能还原照片未展示的侧脸、耳后和头发结构。大角度转头、张嘴、遮挡仍可能出现变形；本机自然度和实际通话效果须分别验证。摄像头输出为 720×1280 不代表生成模型或微信编码具有同等细节。


## 实验性全身人物驱动（LHM++）

`start_call_video_body_avatar` 将照片重建为可动人物，由摄像头驱动身体和头部姿态。采用 [LHM++](https://github.com/aigc3d/LHM-plusplus) 提交 `906b5d9fb967ab42efb92f6fa55bf22cac86b653`，使用已公开的 `3DAIGC/LHMPP-700M` 权重，固定模型版本 `fc9f73664b9bfcc457e96210ddc06fe7caf0d559`。主权重 SHA-256 为 `1897beb3c1ab8f0e198892f9813870aa82d2ada7ec4d043bb8cb293e1d65b725`，人体先验版本为 `3DAIGC/LHMPP-Prior@b683c8f68bede4f318b0bb539730b8e6711d30a0`。本次未使用尚未公开下载的 PixelShuffle / SMPLX-FREE 新权重。

Windows Python 3.12 工作进程用 MediaPipe 检测摄像头姿态；独立 WSL Python 3.10 / PyTorch 2.3.0+cu121 工作进程负责重建和渲染。安装项分别见 `requirements-body-tracking.txt` 和 `requirements-body-avatar-linux.txt`，它们不能装进同一个 Python 环境。Linux 侧还需要与 Torch/CUDA 匹配的 PyTorch3D、pointops、diff-gaussian-rasterization 扩展。`gsplat==1.4.0+pt23cu121` 应安装[官方预编译 wheel](https://docs.gsplat.studio/whl/gsplat/)；torch-scatter 使用 [PyG 的对应 wheel](https://data.pyg.org/whl/torch-2.3.0+cu121.html)，FlashAttention 使用官方 2.6.3 的 Torch 2.3 / CUDA 12 / Python 3.10 wheel。Ubuntu 新版 glibc 与 CUDA 12.1 编译头可能需要独立兼容适配；不能直接把系统 CUDA 13 当作 CUDA 12.1 编译环境。

第三方源码、主权重、人体先验以及 `checkpoints/mediapipe/pose_landmarker_full.task` 均放在项目外的 LHM++ 目录。MediaPipe 模型来自 [Google 官方模型](https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task)。运行时只加载本地文件；不自动安装依赖或上传照片。模型和 SMPL-X/FLAME 数据的许可分别遵循上游说明。

```python
stats = ctx.call_video_statistics(arm=True)
if stats['input_width'] and stats['input_height']:
    ctx.start_call_video_body_avatar(
        r'E:\photos\full-body.png',
        python_executable=r'E:\envs\body-tracking\python.exe',
        repository=r'E:\models\LHM-plusplus',
        linux_python_executable='/opt/body-avatar/bin/python',
        wsl_distribution='Ubuntu',
        width=stats['input_width'], height=stats['input_height'],
        camera_id=0, fps=15, quality='high', tracking_mode='body',
        expected_session=message.session_id,
    )
ctx.stop_call_video()
```

摄像头需要看见肩膀和腰部，手臂动作还需看见肘和手腕。身体姿态按骨骼层级转换成 SMPL-X 关节旋转，应用置信度门限与平滑；不可见关节保持最近姿态。丢失身体时保留人物并标注 `WAITING FOR BODY`，不会把原摄像头画面作为人物源。`stats['ai']['body_tracking']` 和 `tracked_frames` 表示身体跟踪状态；共享协议中的 `faces` / `swapped_frames` 是兼容别名。

重建仅执行一次，随后缓存人物外观并释放重建编码器。测试显卡为 RTX 4060 Laptop 8GB，420×420 渲染的 50 帧标准动作测试中位耗时约 115 ms，运行显存约 2.8GB；这不是端到端通话帧率保证。原始 FP16 路径出现非有限外观张量，因此适配使用全精度并检查结果。推理权重省略了训练身份损失和该解码器未调用的继承模块，适配移除这些未使用模块后仍严格加载其余权重。

Windows 侧仅把姿态参数发给 WSL，不传原始摄像头像素。WSL 返回有界 I420/JPEG 帧，时间戳沿用 Windows 采集时钟。沿用原有会话校验、500 ms 过期检查、三槽共享内存和 Windows Job Object；Linux 侧监听父进程管道 EOF，在停止或父进程退出时释放 GPU。输出始终带测试标记。本机预览和微信发送复用同一帧源，但本机测试不等于真实通话已验证。

当前实现驱动身体、转头和四肢姿态；手指、眨眼、嘴型及 FLAME 表情尚未接入。这种重建仍可能出现脸部细节模糊、手部变形、遮挡或转身后纹理不一致。优先使用正面、手脚完整、简单衣着和干净背景的全身照片，多视角与真实通话效果需进一步验证。


只需摄像头看见脸时，传入 `tracking_mode='head'`。该模式保持身体默认站姿，用 Face Landmarker 的三维变换矩阵驱动颈部和头部，忽略脸在画面中的平移及缩放；按初始朝向校准，并限制、平滑转头幅度。需要在外部模型目录增加 `checkpoints/mediapipe/face_landmarker.task`，来自 [Google 官方模型](https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task)。本机预览使用上半身取景，并可在正面自然表情时点击重新校准。`head_tracking` 表示是否检测到人脸；该模式不要求身体入镜，也不等于已接入眨眼、嘴型或手指动作。

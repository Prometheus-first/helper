"""群音色菜单和切换；模型回复由共享语音层发送。"""
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.summary import split_text
from wxbot_framework.tts import TTSError


def menu_query(value):
    """支持页码、关键词，以及“关键词 页码”。"""
    if len(value) > 150:
        raise TTSError('音色搜索词过长')
    parts = value.rsplit(maxsplit=1)
    page, keyword = 1, value.strip()
    if parts and parts[-1].isascii() and parts[-1].isdigit():
        if len(parts[-1]) > 4:
            raise TTSError('音色页码应为 1–1000 的整数')
        page = int(parts[-1])
        keyword = parts[0].strip() if len(parts) == 2 else ''
    if not 1 <= page <= 1000:
        raise TTSError('音色页码应为 1–1000 的整数')
    return page, keyword


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        ctx.speech.reload()  # 只核对本地配置，首次使用时才请求音色。

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group or message.chat_id not in self.members.groups:
            return
        parts = message.text.strip().split(maxsplit=1)
        if not parts or parts[0] not in ('/查看音色', '/音色'):
            return
        try:
            if parts[0] == '/查看音色':
                query = parts[1].strip() if len(parts) == 2 else ''
                scope = query.split(maxsplit=1)
                all_languages = bool(scope and scope[0] == '全部')
                if all_languages:
                    query = scope[1] if len(scope) == 2 else ''
                page, keyword = menu_query(query)
                voices = ctx.speech.catalog()
                public = ctx.speech.public_catalog(page=page, keyword=keyword, all_languages=all_languages)
                state = self.members.voice_selection(message.chat_id)
                current = state.name if state.enabled else '关闭（文字回复）'
                if state.enabled and not current:
                    try:
                        _, voice = ctx.speech.selected(message.chat_id)
                        current = voice.name
                    except TTSError:
                        current = '未选择（请发送 /音色 名称）'
                sections = [f'本群音色：{current}']
                if page == 1:
                    personal = [voice for voice in voices if not keyword or keyword.casefold() in voice.name.casefold()]
                    names = [voice.name for voice in personal]
                    duplicates = {name for name in names if names.count(name) > 1}
                    lines = [voice.name + (f'（{voice.id}）' if voice.name in duplicates else '') for voice in personal]
                    sections.append('个人音色：\n' + ('、'.join(lines) or '暂无匹配音色'))
                heading = f'{"全部公开" if all_languages else "中文公开"}音色（第 {page} 页）'
                if keyword:
                    heading += f' 搜索：{keyword}'
                lines = []
                public_names = [voice.name.casefold() for voice in public.voices]
                personal_names = {voice.name.casefold() for voice in voices}
                for index, voice in enumerate(public.voices, 1):
                    name = voice.name if len(voice.name) <= 40 else voice.name[:40] + '…'
                    label = f'{index}. {name}'
                    if (len(voice.name) > 40 or public_names.count(voice.name.casefold()) > 1 or
                            voice.name.casefold() in personal_names):
                        label += f'\nID：{voice.id}'
                    lines.append(label)
                sections.append(heading + '：\n' + ('\n'.join(lines) or '本页暂无匹配音色'))
                if public.has_more:
                    prefix = '全部 ' if all_languages else ''
                    sections.append(f'下一页：/查看音色 {prefix}{keyword + " " if keyword else ""}{page + 1}')
                if public.window_limited:
                    sections.append('公开列表受接口浏览范围限制，可按关键词搜索更多音色。')
                sections.append('/查看音色 关键词：搜索\n/查看音色 全部：查看各国语言音色\n/音色 名称：切换\n/音色 关闭：恢复文字回复')
                text = '\n\n'.join(sections)
            elif len(parts) == 1:
                text = '用法：/音色 名称或ID\n/查看音色：查看列表\n/查看音色 关键词：搜索\n/音色 关闭：恢复文字回复'
            else:
                state = ctx.speech.select(message.chat_id, parts[1].strip())
                text = f'本群音色已切换为：{state.name}。' if state.enabled else '本群已恢复文字回复。'
            for part in split_text(text):
                ctx.reply(message, part)
        except TTSError as error:
            ctx.reply(message, str(error))
        except SnapshotExpired:
            ctx.logger.info('音色查询已取消：配置已更新')

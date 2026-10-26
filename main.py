"""会话人格一键切换插件。

把「当前会话用哪个人格」变成群聊 / 私聊里的指令操作：

    /人格列表                查看全部人格和编号
    /人格切换 <序号|名称> [分钟]   切换当前会话人格（可选限时）
    /人格当前                查看当前会话生效的人格
    /人格重置                恢复为全局默认人格

原理：写入 AstrBot 的会话级配置
（shared_preferences，scope="umo"，key="session_service_config"，字段 persona_id），
与 WebUI「会话管理 → 修改人格」是同一处数据，所以两边互通、不会打架。
AstrBot 在每次请求时都会 resolve 人格，因此切换后下一条消息即生效。
"""

import asyncio
import time

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

SCOPE = "umo"
SESSION_CONFIG_KEY = "session_service_config"
PERSONA_FIELD = "persona_id"
SYSTEM_DEFAULT_PERSONA_ID = "default"
SEPARATOR = "-" * 24


class PersonaSwitchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.admin_only = bool(config.get("admin_only", True))
        self.allow_temporary = bool(config.get("allow_temporary", True))
        self.max_temp_minutes = max(1, int(config.get("max_temp_minutes", 720)))
        self.show_prompt_preview = bool(config.get("show_prompt_preview", False))
        self.max_list_rows = max(1, int(config.get("max_list_rows", 50)))

        # umo -> 限时切换任务 / 到期时间戳
        self._temp_tasks: dict[str, asyncio.Task] = {}
        self._temp_deadlines: dict[str, float] = {}

    # ------------------------------------------------------------------ 基础工具

    def _personas(self) -> list[dict]:
        """读取当前已加载的人格列表（Personality 字典列表）。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return []
        return list(getattr(manager, "personas_v3", None) or [])

    def _persona_names(self) -> list[str]:
        return [str(item.get("name", "")) for item in self._personas()]

    async def _session_config(self, umo: str) -> dict:
        data = await sp.get_async(
            scope=SCOPE,
            scope_id=umo,
            key=SESSION_CONFIG_KEY,
            default={},
        )
        return dict(data) if isinstance(data, dict) else {}

    async def _write_session_config(self, umo: str, data: dict) -> None:
        await sp.put_async(
            scope=SCOPE,
            scope_id=umo,
            key=SESSION_CONFIG_KEY,
            value=data,
        )

    async def _set_persona_for_session(self, umo: str, persona_id: str | None) -> None:
        """persona_id 为 None 表示清除会话指定，回落到全局默认人格。"""
        data = await self._session_config(umo)
        if persona_id is None:
            data.pop(PERSONA_FIELD, None)
        else:
            data[PERSONA_FIELD] = persona_id
        await self._write_session_config(umo, data)
        logger.info(
            f"[persona_switch] session={umo} persona -> "
            f"{persona_id if persona_id else '<全局默认>'}"
        )

    async def _effective_persona(self, umo: str) -> tuple[str, str]:
        """返回 (当前生效的人格标识, 来源说明)。"""
        data = await self._session_config(umo)
        forced = data.get(PERSONA_FIELD)
        if forced:
            return str(forced), "本会话指定"
        manager = getattr(self.context, "persona_manager", None)
        fallback = (
            getattr(manager, "default_persona", None) if manager is not None else None
        )
        return str(fallback or SYSTEM_DEFAULT_PERSONA_ID), "全局默认"

    def _match_persona(self, raw: str) -> tuple[dict | None, str]:
        """按序号 / 精确名 / 唯一模糊匹配定位人格。"""
        personas = self._personas()
        if not personas:
            return None, "没读到任何人格，先去 WebUI 建一个吧。"

        query = (raw or "").strip()
        if not query:
            return None, "要切哪个人格？用 /人格列表 看名字。"

        if query.isdigit():
            index = int(query) - 1
            if 0 <= index < len(personas):
                return personas[index], ""
            return None, f"序号超范围，一共 {len(personas)} 个。"

        for item in personas:
            if str(item.get("name")) == query:
                return item, ""

        lowered = query.lower()
        for item in personas:
            if str(item.get("name", "")).lower() == lowered:
                return item, ""

        fuzzy = [i for i in personas if lowered in str(i.get("name", "")).lower()]
        if len(fuzzy) == 1:
            return fuzzy[0], ""
        if len(fuzzy) > 1:
            names = "、".join(str(i.get("name")) for i in fuzzy[:8])
            return None, f"匹配到多个：{names}。麻烦说全名。"

        return None, f"没找到「{query}」，用 /人格列表 看看。"

    # ------------------------------------------------------------------ 限时切换

    def _cancel_temp_task(self, umo: str) -> None:
        task = self._temp_tasks.pop(umo, None)
        self._temp_deadlines.pop(umo, None)
        if task and not task.done():
            task.cancel()

    def _schedule_restore(self, umo: str, old_persona: str | None, minutes: int) -> None:
        self._cancel_temp_task(umo)

        async def _worker(task_holder: list) -> None:
            try:
                await asyncio.sleep(minutes * 60)
                await self._set_persona_for_session(umo, old_persona)
                logger.info(f"[persona_switch] session={umo} 限时人格已到期并还原")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[persona_switch] 还原人格失败：{exc}")
            finally:
                if self._temp_tasks.get(umo) is task_holder[0]:
                    self._temp_tasks.pop(umo, None)
                    self._temp_deadlines.pop(umo, None)

        holder: list = []
        task = asyncio.create_task(_worker(holder))
        holder.append(task)
        self._temp_tasks[umo] = task
        self._temp_deadlines[umo] = time.time() + minutes * 60

    # ------------------------------------------------------------------ 指令

    @filter.command("人格列表", alias={"人格list", "persona_list"})
    async def persona_list(self, event: AstrMessageEvent):
        """查看所有人格及其编号"""
        umo = event.unified_msg_origin
        personas = self._personas()
        if not personas:
            yield event.plain_result("没读到人格，先去 WebUI 建一个吧。")
            return

        current, source = await self._effective_persona(umo)
        lines = [f"当前生效：{current}（{source}）", SEPARATOR]

        for index, item in enumerate(personas[: self.max_list_rows], start=1):
            name = str(item.get("name", "?"))
            mark = "  ← 当前" if name == current else ""
            lines.append(f"{index}. {name}{mark}")

        if len(personas) > self.max_list_rows:
            lines.append(f"…… 共 {len(personas)} 个，只列了前 {self.max_list_rows} 个")

        if self.show_prompt_preview:
            preview = {}
            for item in personas:
                prompt = str(item.get("prompt", "")).replace("\n", " ")
                preview[str(item.get("name"))] = prompt[:60]
            lines.append(SEPARATOR)
            lines.extend(f"{k}: {v}…" for k, v in preview.items())

        lines.append(SEPARATOR)
        lines.append("用法：/人格切换 <序号或名称> [限时分钟]")
        yield event.plain_result("\n".join(lines))

    @filter.command(
        "人格切换",
        alias={"换人格", "切换人格", "人格设置", "persona_switch"},
    )
    async def persona_switch(
        self,
        event: AstrMessageEvent,
        target: str = "",
        minutes: int = 0,
    ):
        """切换当前会话人格：/人格切换 <序号或名称> [限时分钟]"""
        if self.admin_only and not event.is_admin():
            yield event.plain_result("换人格这事只有管理员能拍板。")
            return

        umo = event.unified_msg_origin
        persona, error = self._match_persona(target)
        if persona is None:
            yield event.plain_result(error)
            return

        new_name = str(persona.get("name"))
        old_name = (await self._session_config(umo)).get(PERSONA_FIELD)

        if minutes and minutes > 0:
            if not self.allow_temporary:
                yield event.plain_result("本插件已关闭限时切换，去掉分钟数再试。")
                return
            if minutes > self.max_temp_minutes:
                yield event.plain_result(
                    f"最多只能限时 {self.max_temp_minutes} 分钟。"
                )
                return
            await self._set_persona_for_session(umo, new_name)
            self._schedule_restore(
                umo,
                str(old_name) if old_name else None,
                minutes,
            )
            yield event.plain_result(
                f"本会话已切到「{new_name}」，{minutes} 分钟后自动还原。"
            )
            return

        self._cancel_temp_task(umo)
        await self._set_persona_for_session(umo, new_name)
        yield event.plain_result(f"本会话已切到「{new_name}」，下一条消息生效。")

    @filter.command("人格当前", alias={"当前人格", "人格查看", "persona_current"})
    async def persona_current(self, event: AstrMessageEvent):
        """查看当前会话生效的人格"""
        umo = event.unified_msg_origin
        current, source = await self._effective_persona(umo)
        message = f"当前生效人格：{current}（{source}）"

        deadline = self._temp_deadlines.get(umo)
        if deadline:
            left = max(0, int(round((deadline - time.time()) / 60)))
            message += f"\n限时切换剩余约 {left} 分钟。"
        yield event.plain_result(message)

    @filter.command("人格重置", alias={"人格默认", "清除人格", "persona_reset"})
    async def persona_reset(self, event: AstrMessageEvent):
        """恢复为全局默认人格"""
        if self.admin_only and not event.is_admin():
            yield event.plain_result("这事只有管理员能拍板。")
            return

        umo = event.unified_msg_origin
        self._cancel_temp_task(umo)
        await self._set_persona_for_session(umo, None)

        current, _ = await self._effective_persona(umo)
        yield event.plain_result(
            f"已清掉本会话的人格指定，回到全局默认「{current}」。"
        )

    # ------------------------------------------------------------------ 生命周期

    async def terminate(self):
        """插件卸载 / 重载时清掉限时任务。"""
        for umo in list(self._temp_tasks.keys()):
            self._cancel_temp_task(umo)
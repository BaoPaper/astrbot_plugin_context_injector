from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from string import Template
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_root,
    get_astrbot_skills_path,
)

PLACEHOLDER_RE = re.compile(r"\{\{ctx:([a-zA-Z0-9_.-]+)\}\}")
DEFAULT_BLOCK_TEMPLATE = (
    '<context_template alias="$alias" source="$source_type">\n'
    "$content\n"
    "</context_template>"
)
DEFAULT_TEMPLATE_ERROR = "[上下文模板错误: $alias -> $reason]"
PLUGIN_NAME = "astrbot_plugin_context_injector"
MISSING_BEHAVIOR_MAP = {
    "skip": "skip",
    "跳过": "skip",
    "preserve": "preserve",
    "保留": "preserve",
    "保留原样": "preserve",
    "insert_error": "insert_error",
    "插入错误": "insert_error",
}
BASE_DIR_KEY_MAP = {
    "root": "root",
    "根目录": "root",
    "data": "data",
    "数据目录": "data",
    "skills": "skills",
    "技能目录": "skills",
    "plugin_data": "plugin_data",
    "插件数据目录": "plugin_data",
}


class TemplateRenderError(RuntimeError):
    pass


@register(
    PLUGIN_NAME,
    "BaoPaper",
    "将可复用的文本、文件和命令模板注入到 LLM 请求中。",
    "0.2.3",
)
class ContextInjectorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_data_dir = StarTools.get_data_dir(PLUGIN_NAME).resolve()

    @filter.on_llm_request(priority=-20000)
    async def inject_templates(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._is_enabled():
            return

        cache: dict[str, tuple[bool, str]] = {}
        prompt_injected_aliases: list[str] = []

        if self._expand_prompt_placeholders() and req.prompt:
            req.prompt, prompt_injected_aliases = await self._expand_prompt(
                req.prompt, cache
            )

        append_blocks, append_injected_aliases = await self._render_append_blocks(cache)
        if not append_blocks:
            self._log_injected_templates(prompt_injected_aliases, [])
            return

        suffix = self._append_separator().join(append_blocks)
        if req.system_prompt:
            req.system_prompt = f"{req.system_prompt}{self._append_separator()}{suffix}"
        else:
            req.system_prompt = suffix

        self._log_injected_templates(prompt_injected_aliases, append_injected_aliases)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ctx_preview", alias={"ctxpreview"})
    async def ctx_preview(
        self,
        event: AstrMessageEvent,
        alias: str = "",
    ):
        """预览上下文模板的渲染结果。"""
        if not self._is_enabled():
            yield event.plain_result("上下文模板注入插件当前未启用。")
            return

        cache: dict[str, tuple[bool, str]] = {}
        alias = alias.strip()

        if alias:
            ok, text = await self._render_template(alias, cache)
            if ok:
                yield event.plain_result(text)
                return

            if self._missing_behavior() == "insert_error":
                yield event.plain_result(self._render_error(alias, text))
            else:
                yield event.plain_result(f"模板 `{alias}` 预览失败: {text}")
            return

        append_aliases = self._append_aliases()
        available_aliases = sorted(self._templates().keys())
        append_blocks, _ = await self._render_append_blocks(cache)

        if append_blocks:
            preview = self._append_separator().join(append_blocks)
            message = (
                f"自动注入别名: {', '.join(append_aliases)}\n"
                f"可用模板: {', '.join(available_aliases) if available_aliases else '无'}\n\n"
                f"以下内容会被追加到 system_prompt 末尾:\n\n{preview}"
            )
            yield event.plain_result(message)
            return

        usage = (
            "当前没有可预览的自动注入内容。\n"
            f"自动注入别名: {', '.join(append_aliases) if append_aliases else '无'}\n"
            f"可用模板: {', '.join(available_aliases) if available_aliases else '无'}\n"
            "可使用 /ctx_preview <模板别名> 单独预览某个模板。"
        )
        yield event.plain_result(usage)

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _expand_prompt_placeholders(self) -> bool:
        return bool(self.config.get("expand_prompt_placeholders", True))

    def _append_separator(self) -> str:
        separator = self.config.get("append_separator", "\n\n")
        return separator if isinstance(separator, str) and separator else "\n\n"

    def _missing_behavior(self) -> str:
        behavior = self.config.get("missing_behavior", "skip")
        if isinstance(behavior, str):
            normalized = MISSING_BEHAVIOR_MAP.get(behavior.strip())
            if normalized:
                return normalized
        return "skip"

    def _default_block_template(self) -> str:
        template = self.config.get("default_block_template", DEFAULT_BLOCK_TEMPLATE)
        if isinstance(template, str) and template:
            return template
        return DEFAULT_BLOCK_TEMPLATE

    def _default_error_template(self) -> str:
        template = self.config.get("error_template", DEFAULT_TEMPLATE_ERROR)
        if isinstance(template, str) and template:
            return template
        return DEFAULT_TEMPLATE_ERROR

    def _default_max_chars(self) -> int:
        value = self.config.get("default_max_chars", 12000)
        return value if isinstance(value, int) and value > 0 else 12000

    def _default_command_timeout(self) -> int:
        value = self.config.get("default_command_timeout_sec", 10)
        return value if isinstance(value, int) and value > 0 else 10

    def _append_aliases(self) -> list[str]:
        aliases = self.config.get("append_aliases", [])
        if not isinstance(aliases, list):
            return []
        normalized: list[str] = []
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                normalized.append(alias.strip())
        return normalized

    def _templates(self) -> dict[str, dict[str, Any]]:
        raw_templates = self.config.get("templates", [])
        if not isinstance(raw_templates, list):
            return {}

        templates: dict[str, dict[str, Any]] = {}
        for item in raw_templates:
            if not isinstance(item, dict):
                continue
            if item.get("enabled", True) is False:
                continue
            alias = item.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                continue
            alias = alias.strip()
            if alias in templates:
                logger.warning("检测到重复的上下文模板别名: %s", alias)
            templates[alias] = item
        return templates

    async def _expand_prompt(
        self,
        prompt: str,
        cache: dict[str, tuple[bool, str]],
    ) -> tuple[str, list[str]]:
        aliases = list(
            dict.fromkeys(match.group(1) for match in PLACEHOLDER_RE.finditer(prompt))
        )
        replacements: dict[str, str] = {}
        injected_aliases: list[str] = []

        for alias in aliases:
            replacements[alias], injected = await self._resolve_placeholder(
                alias, cache
            )
            if injected:
                injected_aliases.append(alias)

        def replace_match(match: re.Match[str]) -> str:
            alias = match.group(1)
            return replacements.get(alias, match.group(0))

        return PLACEHOLDER_RE.sub(replace_match, prompt), injected_aliases

    async def _render_append_blocks(
        self,
        cache: dict[str, tuple[bool, str]],
    ) -> tuple[list[str], list[str]]:
        blocks: list[str] = []
        injected_aliases: list[str] = []
        for alias in self._append_aliases():
            ok, text = await self._render_template(alias, cache)
            if ok and text:
                blocks.append(text)
                injected_aliases.append(alias)
                continue

            if self._missing_behavior() == "insert_error":
                blocks.append(self._render_error(alias, text))
        return blocks, injected_aliases

    async def _resolve_placeholder(
        self,
        alias: str,
        cache: dict[str, tuple[bool, str]],
    ) -> tuple[str, bool]:
        ok, text = await self._render_template(alias, cache)
        if ok:
            return text, True

        behavior = self._missing_behavior()
        if behavior == "preserve":
            return f"{{{{ctx:{alias}}}}}", False
        if behavior == "insert_error":
            return self._render_error(alias, text), False
        return "", False

    def _log_injected_templates(
        self,
        prompt_aliases: list[str],
        append_aliases: list[str],
    ) -> None:
        if not prompt_aliases and not append_aliases:
            return

        parts: list[str] = []
        if prompt_aliases:
            parts.append(f"prompt={','.join(prompt_aliases)}")
        if append_aliases:
            parts.append(f"append={','.join(append_aliases)}")
        logger.info("上下文模板已注入: %s", " | ".join(parts))

    async def _render_template(
        self,
        alias: str,
        cache: dict[str, tuple[bool, str]],
    ) -> tuple[bool, str]:
        if alias in cache:
            return cache[alias]

        templates = self._templates()
        item = templates.get(alias)
        if item is None:
            result = (False, "模板不存在")
            cache[alias] = result
            return result

        try:
            rendered = await self._render_template_item(alias, item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("渲染上下文模板 `%s` 失败: %s", alias, exc)
            result = (False, str(exc))
            cache[alias] = result
            return result

        if not rendered:
            result = (False, "模板渲染结果为空")
            cache[alias] = result
            return result

        result = (True, rendered)
        cache[alias] = result
        return result

    async def _render_template_item(self, alias: str, item: dict[str, Any]) -> str:
        source_type = item.get("__template_key")
        if source_type == "text":
            content = item.get("content", "")
            if not isinstance(content, str):
                raise TemplateRenderError("文本模板内容必须是字符串")
        elif source_type == "file":
            content = await self._read_file_template(item)
        elif source_type == "command":
            content = await self._run_command_template(item)
        else:
            raise TemplateRenderError(f"不支持的模板类型: {source_type}")

        content = content.strip()
        if not content:
            return ""

        block_template = item.get("block_template")
        if not isinstance(block_template, str) or not block_template:
            block_template = self._default_block_template()

        return Template(block_template).safe_substitute(
            alias=alias,
            source_type=source_type,
            content=content,
        )

    async def _read_file_template(self, item: dict[str, Any]) -> str:
        file_path_raw = item.get("path", "")
        if not isinstance(file_path_raw, str) or not file_path_raw.strip():
            raise TemplateRenderError("文件模板路径为空")

        base_dir_key = item.get("base_dir", "skills")
        file_path = self._resolve_path(
            file_path_raw.strip(),
            base_dir_key,
            default_base_dir="skills",
        )
        if not file_path.is_file():
            raise TemplateRenderError(f"文件不存在: {file_path}")

        content = await asyncio.to_thread(
            file_path.read_text,
            encoding="utf-8",
            errors="replace",
        )
        return self._truncate_content(content, item.get("max_chars"))

    async def _run_command_template(self, item: dict[str, Any]) -> str:
        argv = self._build_command_argv(item)

        timeout = item.get("timeout_sec")
        if not isinstance(timeout, int) or timeout <= 0:
            timeout = self._default_command_timeout()

        workdir_base_key = self._normalize_base_dir_key(
            item.get("workdir_base", "root"),
            default="root",
        )
        workdir: Path | None = self._base_dirs()[workdir_base_key]
        workdir_value = item.get("custom_workdir", "")
        if isinstance(workdir_value, str) and workdir_value.strip():
            workdir = self._resolve_path(
                workdir_value.strip(),
                workdir_base_key,
                expect_file=False,
                default_base_dir="root",
            )
        if not workdir.exists() or not workdir.is_dir():
            raise TemplateRenderError(f"工作目录不存在: {workdir}")

        process = await asyncio.create_subprocess_exec(
            argv[0],
            *argv[1:],
            cwd=str(workdir) if workdir else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise TemplateRenderError(
                f"命令执行超时，已超过 {timeout} 秒",
            ) from exc

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        output = output.strip()
        if process.returncode not in (0, None):
            raise TemplateRenderError(
                f"命令退出码为 {process.returncode}: {output or '无输出'}",
            )
        return self._truncate_content(output, item.get("max_chars"))

    def _build_command_argv(self, item: dict[str, Any]) -> list[str]:
        executable = item.get("executable", "")
        if isinstance(executable, str) and executable.strip():
            argv = [executable.strip()]
            raw_args = item.get("args", [])
            if raw_args is None:
                raw_args = []
            if not isinstance(raw_args, list):
                raise TemplateRenderError("命令模板的 args 必须是字符串列表")
            for arg in raw_args:
                if not isinstance(arg, str):
                    raise TemplateRenderError("命令模板的 args 必须是字符串列表")
                argv.append(arg)
            return argv

        command = item.get("command", "")
        if not isinstance(command, str) or not command.strip():
            raise TemplateRenderError("命令模板至少需要提供 executable 或 command")

        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            raise TemplateRenderError(f"命令解析失败: {exc}") from exc

        if not argv:
            raise TemplateRenderError("命令模板解析后为空")
        return argv

    def _truncate_content(self, content: str, override: Any) -> str:
        limit = (
            override
            if isinstance(override, int) and override > 0
            else self._default_max_chars()
        )
        if len(content) <= limit:
            return content
        return f"{content[:limit]}\n... [已截断到 {limit} 个字符]"

    def _resolve_path(
        self,
        raw_path: str,
        base_dir_key: Any,
        *,
        expect_file: bool = True,
        default_base_dir: str = "skills",
    ) -> Path:
        candidate = Path(raw_path)
        roots = self._allowed_roots()
        normalized_base_dir_key = self._normalize_base_dir_key(
            base_dir_key,
            default=default_base_dir,
        )

        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            return self._ensure_in_allowed_roots(
                resolved, roots, expect_file=expect_file
            )

        base_dir = self._base_dirs().get(
            normalized_base_dir_key, self._base_dirs()["skills"]
        )
        resolved = (base_dir / candidate).resolve(strict=False)
        return self._ensure_under_root(resolved, base_dir, expect_file=expect_file)

    def _normalize_base_dir_key(
        self, base_dir_key: Any, default: str = "skills"
    ) -> str:
        if isinstance(base_dir_key, str):
            normalized = BASE_DIR_KEY_MAP.get(base_dir_key.strip())
            if normalized:
                return normalized
        return default

    def _base_dirs(self) -> dict[str, Path]:
        return {
            "root": Path(get_astrbot_root()).resolve(),
            "data": Path(get_astrbot_data_path()).resolve(),
            "skills": Path(get_astrbot_skills_path()).resolve(),
            "plugin_data": self.plugin_data_dir,
        }

    def _allowed_roots(self) -> list[Path]:
        base_dirs = self._base_dirs()
        return [
            base_dirs["root"],
            base_dirs["data"],
            base_dirs["skills"],
            base_dirs["plugin_data"],
        ]

    def _ensure_in_allowed_roots(
        self,
        candidate: Path,
        roots: list[Path],
        *,
        expect_file: bool,
    ) -> Path:
        for root in roots:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        target_type = "文件" if expect_file else "路径"
        allowed = "、".join(str(root) for root in roots)
        raise TemplateRenderError(
            f"{target_type}超出了允许的目录范围: {candidate}；允许的目录: {allowed}",
        )

    def _ensure_under_root(
        self,
        candidate: Path,
        root: Path,
        *,
        expect_file: bool,
    ) -> Path:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            target_type = "文件" if expect_file else "路径"
            raise TemplateRenderError(
                f"{target_type}越过了基准目录限制: {candidate}",
            ) from exc
        return candidate

    def _render_error(self, alias: str, reason: str) -> str:
        return Template(self._default_error_template()).safe_substitute(
            alias=alias,
            reason=reason,
        )

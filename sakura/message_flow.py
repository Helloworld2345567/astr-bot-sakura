"""Sakura burst coalescing, control messages and user-owned image references."""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from astrbot.api.event import filter
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.utils.media_utils import MediaResolver


def is_control(text: str) -> bool:
    return bool(re.match(r"^\s*[/／]\S", text or "") or re.search(r"\[[^\]]*\]", text or ""))


def wants_reference(text: str) -> bool:
    text = text or ""
    if re.search(r"(?:不要|不用|无需|别|不需要)(?:使用|用)?(?:参考图|参照图|图生图)", text):
        return False
    action = re.search(
        r"生图|生成|重画|重绘|画(?:[一张出成幅个]|\s|$)|改成|换成|替换|修改|编辑|模仿|也来|来一|来个|edit|generate|draw", text, re.I
    )
    return bool(action and re.search(
        r"参考图|参照图|以图生图|图生图|编辑.{0,6}(?:图|照片)|"
        r"(?:按|照|根据|参考|模仿|沿用|保持|用).{0,24}(?:图|照片|动作|姿势|构图)|"
        r"(?:这|那|上|刚|前).{0,6}(?:图|照片).{0,20}(?:改|画|生成|模仿)|"
        r"(?:edit\s+(?:this|the)\s+(?:image|photo)|reference\s+image)",
        text, re.I,
    ))


class IncomingControlFilter(filter.CustomFilter):
    """Capture before command handlers can reply or strip the wake prefix."""
    target_persona_id = "猫娘小樱"

    def filter(self, event, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "message_str", "") or event.get_message_str()
        event.set_extra("_sakura_control", is_control(raw) or is_control(event.get_message_str()))
        # Bracketed slash commands still use AstrBot's real command handlers.
        runner = cfg.get("agent_runner", {}).get("config", {})
        persona = runner.get("persona", {}).get("persona_id", runner.get("persona_id"))
        match = re.fullmatch(r"\s*\[\s*[/／]([^\[\]]+)\s*\]\s*", event.get_message_str())
        if match and persona == self.target_persona_id:
            event.message_str = match[1].strip()
        return True


@dataclass
class Snapshot:
    event: object
    text: str
    chain: list
    images: list[Image] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    image_error: bool = False


class MessageFlow:
    def __init__(self, window: float = 2.0):
        self.window = window
        self.pending: dict[tuple, list[Snapshot]] = {}
        self.references: OrderedDict[tuple, tuple[float, list[Image], int]] = OrderedDict()
        self.max_cache_bytes = 32 * 1024 * 1024
        self.reference_ttl = 600

    @staticmethod
    def sender_key(event):
        return (event.unified_msg_origin, str(event.get_sender_id()))

    def key(self, event):
        return (*self.sender_key(event), event.get_extra("_sakura_conversation_id"))

    def interrupt(self, event):
        # A control message must never be delayed behind a pending chat batch.
        for key in list(self.pending):
            if key[:2] == self.sender_key(event):
                for snap in self.pending.pop(key):
                    snap.event.stop_event()

    def bind_conversation(self, event, cid):
        old_key = self.key(event)
        if cid and old_key[-1] is None:
            event.set_extra("_sakura_conversation_id", cid)
            cached = self.references.pop(old_key, None)
            if cached:
                self.references[self.key(event)] = cached

    def _prune(self):
        now = time.monotonic()
        for key, (stamp, _, _) in list(self.references.items()):
            if now - stamp > self.reference_ttl:
                del self.references[key]
        while sum(item[2] for item in self.references.values()) > self.max_cache_bytes or len(self.references) > 32:
            self.references.popitem(last=False)

    async def _snapshot_images(self, snap):
        # Own the bytes before a superseded event's pipeline cleans up files.
        async def copy_image(comp):
            async with asyncio.timeout(20):
                resolver = MediaResolver(comp.url or comp.file, media_type="image")
                async with resolver.as_path() as resolved:
                    if not 0 < resolved.path.stat().st_size <= 10 * 1024 * 1024:
                        raise ValueError("reference image exceeds the input size limit")
                    return Image.fromBytes(resolved.read_bytes())

        try:
            copied = []
            for comp in snap.chain:
                if isinstance(comp, Image):
                    image = await copy_image(comp)
                    copied.append(image)
                    snap.images.append(image)
                elif isinstance(comp, Reply):
                    comp = comp.model_copy(deep=True)
                    chain = []
                    for child in comp.chain or []:
                        if isinstance(child, Image):
                            child = await copy_image(child)
                            snap.images.append(child)
                        chain.append(child)
                    comp.chain = chain
                    copied.append(comp)
                else:
                    copied.append(comp)
                if len(snap.images) > 4:
                    raise ValueError("too many reference images")
            snap.chain = copied
        except Exception:
            snap.image_error = True
        finally:
            snap.ready.set()

    async def collect(self, event):
        key = self.key(event)
        snap = Snapshot(event, event.get_message_str(), list(event.get_messages()))
        batch = self.pending.setdefault(key, [])
        batch.append(snap)
        if len(batch) > 2:
            batch.pop(0).event.stop_event()
        try:
            await self._snapshot_images(snap)
            await asyncio.sleep(self.window)
            if self.pending.get(key) is not batch or batch[-1] is not snap or event.is_stopped():
                event.stop_event()
                return
            # A previous image download may still be completing.
            for entry in list(batch):
                await entry.ready.wait()
            if self.pending.get(key) is not batch or batch[-1] is not snap or event.is_stopped():
                event.stop_event()
                return
            self.pending.pop(key, None)
            text = "\n".join(entry.text for entry in batch if entry.text.strip())
            event.message_str = text
            event.message_obj.message_str = text
            event.message_obj.message = [
                item for index, entry in enumerate(batch)
                for item in ([Plain(text="\n")] if index else []) + entry.chain
            ]
            event.set_extra("_sakura_merged_count", len(batch))
            event.set_extra("_sakura_reference_error", any(entry.image_error for entry in batch))
            images = [image for entry in batch for image in entry.images]
            self._prune()
            if event.get_extra("_sakura_reference_error", False):
                self.references.pop(key, None)
                images = []
            elif images:
                size = sum(len(image.file or "") for image in images)
                self.references[key] = (time.monotonic(), images, size)
                self.references.move_to_end(key)
                self._prune()
            elif key in self.references:
                images = self.references[key][1]
            event.set_extra("_sakura_reference_images", images)
        except BaseException:
            if self.pending.get(key) is batch and batch[-1] is snap:
                self.pending.pop(key, None)
            raise

    def clear(self):
        for batch in self.pending.values():
            for snap in batch:
                snap.event.stop_event()
        self.pending.clear()
        self.references.clear()


def select_references(images: list[Image], text: str) -> list[Image]:
    # Explicit ordinals must select that input, never a random available image.
    ordinals = re.findall(r"第\s*([一二三四五六七八九十\d]+)\s*(?:张|幅|个)(?:图|照片)?", text or "")
    if ordinals:
        selected = []
        for ordinal in ordinals:
            index = "一二三四五六七八九十".find(ordinal) + 1 if len(ordinal) == 1 and ordinal in "一二三四五六七八九十" else int(ordinal) if ordinal.isdecimal() else 0
            if not 1 <= index <= len(images):
                return []
            if index not in selected:
                selected.append(index)
        return [images[index - 1] for index in selected]
    if re.search(r"最后一张|后一张", text or ""):
        return images[-1:]
    return images

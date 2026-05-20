from __future__ import annotations

import os


def _patch_karaoke_gen_output_config() -> None:
    if os.getenv("KARAOKE_FORGE_PATCH_KARAOKE_GEN") != "1":
        return

    raw_offset = os.getenv("KARAOKE_DEFAULT_SUBTITLE_OFFSET_MS")
    if raw_offset is None or not raw_offset.strip():
        return

    try:
        forced_offset = int(raw_offset.strip())
    except ValueError:
        return

    try:
        from karaoke_gen.lyrics_transcriber.core.config import OutputConfig
    except Exception:
        return

    if getattr(OutputConfig, "_karaoke_forge_patched", False):
        return

    original_init = OutputConfig.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # karaoke-gen passes subtitle_offset_ms into prep, but its after-review
        # final render path rebuilds OutputConfig without that field. If Forge
        # supplied an offset and the rebuilt config falls back to zero, restore
        # the Forge value so final video rendering matches the run command.
        if getattr(self, "subtitle_offset_ms", 0) == 0 and forced_offset != 0:
            self.subtitle_offset_ms = forced_offset

    OutputConfig.__init__ = patched_init
    OutputConfig._karaoke_forge_patched = True


_patch_karaoke_gen_output_config()

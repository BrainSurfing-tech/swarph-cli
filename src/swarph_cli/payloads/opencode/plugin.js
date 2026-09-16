// swarph-opencode.js — the OpenCode plugin that wires swarph hooks into opencode.
//
// opencode has no command-hook surface; its ONLY hook surface is a JS plugin
// loaded from the config dir's plugins/ tree (auto-loaded, no opencode.json
// `plugin` entry — measured: a bare file in the plugin dir loads with a config
// containing only `$schema`). This file is a rendered package payload: the
// installer (and the opencode membrane's `_opencode_env`) replace `@PYTHON@`
// with the pinned swarph interpreter and drop it into the active config dir.
//
// Hook -> swarph product map (read from the @opencode-ai/plugin Hook types):
//   experimental.chat.system.transform   -> `hook-output` (starter) + one
//       `wake-hook-output --harness opencode` boot injection, appended to
//       output.system[] once per session.
//   experimental.session.compacting       -> `postcompact-hook-output` (7-day
//       timeline recall), pushed onto output.context[].
//   tool.execute.before                   -> `hooks touch-activity` (the
//       watchdog liveness marker, the codex PreToolUse analog).
//
// Failure-mode invariant (the house rule for every hook callback): a shell-out
// that throws returns "" and the hook injects nothing. The plugin MUST NOT
// crash an opencode turn because swarph is absent or the timeline unreachable —
// worst case is no injection, never a refused turn.
import { execFileSync } from "node:child_process";

const PY = @PYTHON@;
const TIMEOUT_MS = 15000;

function run(verb, args = []) {
  try {
    return execFileSync(
      PY,
      ["-m", "swarph_cli", verb, ...args],
      { encoding: "utf8", timeout: TIMEOUT_MS, input: "" },
    ).trim();
  } catch (_err) {
    return "";
  }
}

function additionalContext(json) {
  try {
    const parsed = JSON.parse(json);
    const ctx = parsed && parsed.hookSpecificOutput
      ? parsed.hookSpecificOutput.additionalContext
      : undefined;
    return typeof ctx === "string" ? ctx.trim() : "";
  } catch (_err) {
    return "";
  }
}

export const SwarphOpencodePlugin = async () => {
  let bootDone = false;
  return {
    "experimental.chat.system.transform": async (_input, output) => {
      if (bootDone) return;
      bootDone = true;
      const additions = [];
      // `hook-output` no-ops under SWARPH_SPAWN=1 (spawn already injected the
      // starter via `--prompt`), so in a cell only the wake-arm lands; in a bare
      // `opencode` session both land. Either way it is once-per-session.
      const starter = additionalContext(run("hook-output"));
      if (starter) additions.push(starter);
      const wake = additionalContext(run("wake-hook-output", ["--harness", "opencode"]));
      if (wake) additions.push(wake);
      for (const text of additions) {
        if (Array.isArray(output.system)) output.system.push(text);
      }
    },
    "experimental.session.compacting": async (_input, output) => {
      const recall = additionalContext(run("postcompact-hook-output"));
      if (recall) {
        if (Array.isArray(output.context)) output.context.push(recall);
      }
    },
    "tool.execute.before": async (_input) => {
      run("hooks", ["touch-activity"]);
    },
  };
};

export default SwarphOpencodePlugin;

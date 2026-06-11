#!/bin/bash -l
# Launch wrapper run AS the tmux session's command by claude-tmux.service.
# Login shell (-l) guarantees full PATH/env so the provider binary resolves
# (e.g. on WSL where `claude` is a claude.exe symlink under interop).
# exec's swarph spawn, which exec-replaces with the resumed cell session.
#
# Install at <HOME>/.config/swarph/launch-<PEER>.sh, chmod +x, substitute <PEER>/<HOME>.
exec <HOME>/.local/bin/swarph spawn <PEER>

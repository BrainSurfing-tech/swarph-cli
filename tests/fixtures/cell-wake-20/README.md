The idle fixture is the unchanged tail (finished-turn header through footer)
of the commander's real cell-opencode capture at 2026-11-15 12:36Z, supplied
by lab-ovh in DM98584 as
`/var/tmp/opencode-fixtures/opencode_idle_after_turn_real_20261115.txt`.
It retains the actual `Build auto` mode row. No synthetic running marker is added.

`running-real-20261115.txt` is the full, unchanged commander capture at
2026-11-15 13:53:55Z, supplied by lab-ovh in DM98603 as
`/var/tmp/opencode-fixtures/opencode_running_turn_real_20261115.txt`.
It is the same session, with the actual interrupt footer and bare current
Build header below the finished header. Lab checked the capture for secrets.

The existing `../pr514-hunt-794/o_B1_permission_real_verbatim.txt` is the
real permission-dialog busy capture already tested by test_961_cursor_grok_footer.

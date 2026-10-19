.PHONY: egress-probe

# card #1225: the whole tests/ tree except the probe's own suite, which
# launches the unlisted-connect mutants and would trip the outer trace.
# IP-literal TCP only. GNU make turns any recipe failure into exit 2, so
# the status line is the probe's own code (1 leak, 2 isolation-unproven).
egress-probe:
	@python3 tests/egress_probe.py --pytest tests \
		--ignore=tests/test_1219_egress_probe.py \
		--ignore=tests/egress_probe.py; \
	status=$$?; \
	echo "egress-probe-status $$status"; \
	exit $$status

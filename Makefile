.PHONY: egress-probe

# card #1219: smoke subset inside bwrap --unshare-net. Exit 2 names the
# reason when isolation cannot be proven.
egress-probe:
	python3 tests/egress_probe.py --smoke

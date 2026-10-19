.PHONY: egress-probe

# card #1219: real swarph-cli tests inside bwrap --unshare-net.
# IP-literal TCP only; a hostname that never leaves the stub resolver
# is not judged. Exit 2 names the reason when isolation cannot be proven.
egress-probe:
	python3 tests/egress_probe.py --pytest tests/test_1214_egress.py tests/test_1071_quota_sync.py

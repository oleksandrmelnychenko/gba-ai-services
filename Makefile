SERVICES := gba-pricing gba-reco gba-procure gba-nba gba-solvency
CALIBRATION_SERVICES := gba-reco gba-procure gba-nba
SMOKE_SERVICES := gba-pricing gba-reco gba-solvency

.PHONY: install lint test integration calibration smoke static-check release-check live-check

install:
	set -e; for service in $(SERVICES); do $(MAKE) -C $$service install; done

lint:
	set -e; for service in $(SERVICES); do $(MAKE) -C $$service lint; done

test:
	set -e; for service in $(SERVICES); do $(MAKE) -C $$service test; done

integration:
	set -e; for service in $(SERVICES); do $(MAKE) -C $$service integration; done

calibration:
	set -e; for service in $(CALIBRATION_SERVICES); do $(MAKE) -C $$service calibration; done

smoke:
	set -e; for service in $(SMOKE_SERVICES); do $(MAKE) -C $$service smoke; done

static-check: lint test

release-check: static-check calibration

live-check: integration smoke

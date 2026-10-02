.PHONY: check release deploy

check:
	python3 -m unittest discover -s tests -v
	python3 -m compileall -q custom_components scripts
	node --check custom_components/central_heating/www/central-heating-card.js
	bash -n scripts/install.sh

release: check
	python3 scripts/build_release.py

deploy: release
	python3 scripts/deploy.py


SITE_JS := ../brockmuellers.github.io/assets/js

.PHONY: serve deploy test

serve:
	python3 -m http.server 8000

deploy:
	@test -d $(SITE_JS) || { echo "Error: $(SITE_JS) not found — is brockmuellers.github.io checked out next to this repo?"; exit 1; }
	cp web/app.js $(SITE_JS)/acb-calculator.js
	@echo "Deployed app.js → $(SITE_JS)/acb-calculator.js"

test:
	python3 -m pytest

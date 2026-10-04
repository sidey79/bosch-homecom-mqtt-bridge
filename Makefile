.PHONY: test verify image compose

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -t . -v

verify:
	python3 -m compileall -q src tests
	PYTHONPATH=src python3 -m unittest discover -s tests -t .
	docker compose config --quiet

image:
	docker build -t bosch-homecom-mqtt-bridge .

compose:
	docker compose up --build

UV_BIN ?= $(HOME)/.local/bin/uv
PYTHON_VERSION ?= 3.13
SERVICE_USER ?= $(shell id -un)
SERVICE_GROUP ?= $(shell id -gn)
STATE_DIR ?= /var/lib/commuter
CONFIG_DIR ?= /etc/commuter
ENV_FILE ?= $(CONFIG_DIR)/commuter.env
SYSTEMD_DIR ?= /etc/systemd/system
PROJECT_ROOT := $(realpath $(CURDIR))

.PHONY: install

# Run on the Raspberry Pi from any already-copied or git-pulled checkout.
# It never replaces the encrypted database, Fernet key, or populated env file.
install:
	test -x "$(UV_BIN)"
	"$(UV_BIN)" sync --frozen --no-dev --python "$(PYTHON_VERSION)"
	sudo install -d -o "$(SERVICE_USER)" -g "$(SERVICE_GROUP)" -m 0700 "$(STATE_DIR)"
	sudo chown -R "$(SERVICE_USER):$(SERVICE_GROUP)" "$(STATE_DIR)"
	sudo install -d -o root -g "$(SERVICE_GROUP)" -m 0750 "$(CONFIG_DIR)"
	@if ! sudo test -f "$(ENV_FILE)"; then \
		sudo install -m 0640 -o root -g "$(SERVICE_GROUP)" deploy/systemd/commuter.env.example "$(ENV_FILE)"; \
		echo "Created $(ENV_FILE); set its secrets, then run make install again."; \
		exit 1; \
	fi
	sed -e 's|__COMMUTER_PROJECT_ROOT__|$(PROJECT_ROOT)|g' -e 's|__COMMUTER_SERVICE_USER__|$(SERVICE_USER)|g' -e 's|__COMMUTER_SERVICE_GROUP__|$(SERVICE_GROUP)|g' deploy/systemd/commuter-web.service | sudo tee "$(SYSTEMD_DIR)/commuter-web.service" >/dev/null
	sed -e 's|__COMMUTER_PROJECT_ROOT__|$(PROJECT_ROOT)|g' -e 's|__COMMUTER_SERVICE_USER__|$(SERVICE_USER)|g' -e 's|__COMMUTER_SERVICE_GROUP__|$(SERVICE_GROUP)|g' deploy/systemd/commuter-sync.service | sudo tee "$(SYSTEMD_DIR)/commuter-sync.service" >/dev/null
	sudo install -m 0644 deploy/systemd/commuter-sync.timer "$(SYSTEMD_DIR)/commuter-sync.timer"
	sudo systemctl daemon-reload
	sudo systemctl enable commuter-sync.timer
	sudo systemctl start commuter-sync.timer
	sudo systemctl try-restart commuter-web.service
	sudo systemctl try-restart commuter-sync.timer

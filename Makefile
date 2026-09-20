UV_BIN ?= $(HOME)/.local/bin/uv
SERVICE_USER ?= commuter
STATE_DIR ?= /var/lib/commuter
CONFIG_DIR ?= /etc/commuter
ENV_FILE ?= $(CONFIG_DIR)/commuter.env
SYSTEMD_DIR ?= /etc/systemd/system

.PHONY: install

# Run on the Raspberry Pi from its already-copied or git-pulled checkout.
# It never replaces the encrypted database, Fernet key, or populated env file.
install:
	test -x "$(UV_BIN)"
	sudo "$(UV_BIN)" sync --frozen --no-dev
	@id -u "$(SERVICE_USER)" >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir "$(STATE_DIR)" --create-home --shell /usr/sbin/nologin "$(SERVICE_USER)"
	sudo install -d -o "$(SERVICE_USER)" -g "$(SERVICE_USER)" -m 0700 "$(STATE_DIR)"
	sudo install -d -o root -g "$(SERVICE_USER)" -m 0750 "$(CONFIG_DIR)"
	@if ! sudo test -f "$(ENV_FILE)"; then \
		sudo install -m 0640 -o root -g "$(SERVICE_USER)" deploy/systemd/commuter.env.example "$(ENV_FILE)"; \
		echo "Created $(ENV_FILE); set its secrets, then run make install again."; \
		exit 1; \
	fi
	sudo install -m 0644 deploy/systemd/commuter-web.service "$(SYSTEMD_DIR)/commuter-web.service"
	sudo install -m 0644 deploy/systemd/commuter-sync.service "$(SYSTEMD_DIR)/commuter-sync.service"
	sudo install -m 0644 deploy/systemd/commuter-sync.timer "$(SYSTEMD_DIR)/commuter-sync.timer"
	sudo systemctl daemon-reload
	sudo systemctl enable --now commuter-sync.timer
	sudo systemctl try-restart commuter-web.service
	sudo systemctl try-restart commuter-sync.timer

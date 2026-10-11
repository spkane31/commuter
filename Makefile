UV_BIN ?= $(HOME)/.local/bin/uv
PYTHON_VERSION ?= 3.13
SERVICE_USER ?= $(shell id -un)
SERVICE_GROUP ?= $(shell id -gn)
STATE_DIR ?= /var/lib/commuter
CONFIG_DIR ?= /etc/commuter
ENV_FILE ?= $(CONFIG_DIR)/commuter.env
SYSTEMD_DIR ?= /etc/systemd/system
PROJECT_ROOT := $(realpath $(CURDIR))

.PHONY: install install-temporal-worker

# Run on the Raspberry Pi from any already-copied or git-pulled checkout.
# It never replaces the owner-only plaintext database or populated env file.
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

# Install and start the separate Temporal Cloud webhook consumer on a Linux host.
# Keep this opt-in so existing Pi installs and the polling timer are unaffected.
install-temporal-worker:
	@test -x "$(UV_BIN)"
	"$(UV_BIN)" sync --frozen --no-dev --python "$(PYTHON_VERSION)"
	sudo install -d -o "$(SERVICE_USER)" -g "$(SERVICE_GROUP)" -m 0700 "$(STATE_DIR)"
	sudo install -d -o root -g "$(SERVICE_GROUP)" -m 0750 "$(CONFIG_DIR)"
	@if ! sudo test -f "$(ENV_FILE)"; then \
		sudo install -m 0640 -o root -g "$(SERVICE_GROUP)" deploy/systemd/commuter.env.example "$(ENV_FILE)"; \
		echo "Created $(ENV_FILE); configure the worker settings, then run make install-temporal-worker again."; \
		exit 1; \
	fi
	@sudo grep -Eq '^TEMPORAL_ADDRESS=.+$$' "$(ENV_FILE)" || { echo "Set TEMPORAL_ADDRESS in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^TEMPORAL_NAMESPACE=.+$$' "$(ENV_FILE)" || { echo "Set TEMPORAL_NAMESPACE in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^TEMPORAL_API_KEY=.+$$' "$(ENV_FILE)" || { echo "Set TEMPORAL_API_KEY in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^STRAVA_CLIENT_ID=.+$$' "$(ENV_FILE)" || { echo "Set STRAVA_CLIENT_ID in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^STRAVA_CLIENT_SECRET=.+$$' "$(ENV_FILE)" || { echo "Set STRAVA_CLIENT_SECRET in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^COMMUTER_SHEETS_ENABLED=true$$' "$(ENV_FILE)" || { echo "Set COMMUTER_SHEETS_ENABLED=true in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^COMMUTER_SPREADSHEET_ID=.+$$' "$(ENV_FILE)" || { echo "Set COMMUTER_SPREADSHEET_ID in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^GOOGLE_APPLICATION_CREDENTIALS=.+$$' "$(ENV_FILE)" || { echo "Set GOOGLE_APPLICATION_CREDENTIALS in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^DISCORD_BOT_TOKEN=.+$$' "$(ENV_FILE)" || { echo "Set DISCORD_BOT_TOKEN in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^DISCORD_GUILD_ID=.+$$' "$(ENV_FILE)" || { echo "Set DISCORD_GUILD_ID in $(ENV_FILE)."; exit 1; }
	@sudo grep -Eq '^DISCORD_CHANNEL_ID=.+$$' "$(ENV_FILE)" || { echo "Set DISCORD_CHANNEL_ID in $(ENV_FILE)."; exit 1; }
	sudo chown root:"$(SERVICE_GROUP)" "$(ENV_FILE)"
	sudo chmod 0640 "$(ENV_FILE)"
	sed -e 's|__COMMUTER_PROJECT_ROOT__|$(PROJECT_ROOT)|g' -e 's|__COMMUTER_SERVICE_USER__|$(SERVICE_USER)|g' -e 's|__COMMUTER_SERVICE_GROUP__|$(SERVICE_GROUP)|g' -e 's|__COMMUTER_ENV_FILE__|$(ENV_FILE)|g' deploy/systemd/commuter-temporal-worker.service | sudo tee "$(SYSTEMD_DIR)/commuter-temporal-worker.service" >/dev/null
	sudo systemd-analyze verify "$(SYSTEMD_DIR)/commuter-temporal-worker.service"
	sudo systemctl daemon-reload
	sudo systemctl enable --now commuter-temporal-worker.service
	sudo systemctl try-restart commuter-temporal-worker.service
	sudo systemctl status --no-pager commuter-temporal-worker.service

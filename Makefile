SHELL := bash
export PYTHON ?= $(CURDIR)/backend/.venv/bin/python
export PROVIDER_MODE := offline

.PHONY: dev-check dev-up dev-status dev-audio-smoke dev-down test-offline test-transport-unit test-transport-app

dev-check:
	bash scripts/dev-harness/dev-check.sh

dev-up:
	bash scripts/dev-harness/dev-up.sh

dev-status:
	bash scripts/dev-harness/dev-status.sh

dev-audio-smoke:
	bash scripts/dev-harness/audio-capture-smoke.sh

dev-down:
	bash scripts/dev-harness/dev-down.sh

test-transport-unit:
	$(MAKE) test-docker
	cd scripts && "$${PYTHON}" -m unittest local_live_preview.test_reset_retention local_live_preview.test_parakeet local_live_preview.test_diarization_proxy
	$(MAKE) test-library
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider tests/unit/test_local_transcript.py tests/unit/test_offline_route_policy.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_local_stt.py scripts/dev-harness/tests/test_local_stt_watch.py
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider tests/unit/test_local_transport_auth.py tests/unit/test_offline_audio_capture.py
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider tests/unit/test_local_live_preview.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_local_mac.py scripts/dev-harness/tests/test_local_transport.py scripts/dev-harness/tests/test_tailscale_backend.py

test-transport-app:
	cd app && bash test.sh test/providers/conversation_provider_processing_reconcile_test.dart test/widgets/processing_conversation_widget_test.dart
	cd app && bash test.sh test/widgets/transcript_test.dart test/unit/stt_provider_display_test.dart test/unit/transcript_segment_speaker_test.dart test/services/sockets/transcription_service_onboarding_test.dart
	cd app && bash test.sh test/unit/local_mac_session_test.dart test/widgets/local_mac_page_test.dart test/widgets/local_runtime_status_card_test.dart test/unit/offline_network_policy_test.dart test/unit/pure_socket_auth_test.dart test/unit/authenticated_request_401_test.dart test/unit/auth_refresh_timeout_test.dart test/unit/startup_auth_timeout_test.dart test/widgets/session_expired_reauthentication_test.dart test/providers/capture_provider_test.dart test/providers/device_provider_test.dart test/services/local_device_audio_start_test.dart test/widgets/temporary_recording_controls_test.dart
	cd app && bash scripts/analyze_ratchet.sh

test-offline:
	$(MAKE) test-library
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -x -p no:cacheprovider tests/unit/test_local_transcript.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -x -p no:cacheprovider scripts/dev-harness/tests/test_local_stt.py scripts/dev-harness/tests/test_local_stt_watch.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -x -p no:cacheprovider scripts/dev-harness/tests/test_local_mac.py scripts/dev-harness/tests/test_local_transport.py scripts/dev-harness/tests/test_tailscale_backend.py
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -x -p no:cacheprovider tests/unit/test_local_transport_auth.py tests/unit/test_verify_token_admin_and_local_dev_gating.py
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -x -p no:cacheprovider tests/unit/test_offline_audio_capture.py tests/unit/test_local_live_preview.py tests/unit/test_offline_network_policy.py tests/unit/test_offline_route_policy.py tests/unit/test_offline_main_surface.py tests/unit/test_offline_provider_gates.py tests/unit/test_offline_voice_message_routes.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -x -p no:cacheprovider scripts/dev-harness/tests/test_safety.py scripts/dev-harness/tests/test_cli.py scripts/dev-harness/tests/test_env_stage.py

.PHONY: test-library
test-library:
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_local_providers.py scripts/dev-harness/tests/test_local_provider_relay.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_transcription_lock.py scripts/dev-harness/tests/test_live_install.py scripts/dev-harness/tests/test_local_live.py scripts/dev-harness/tests/test_local_transcription.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_install_recovery.py scripts/dev-harness/tests/test_stt_install.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_local_setup.py scripts/dev-harness/tests/test_ios_setup.py scripts/dev-harness/tests/test_ios_scene_lifecycle.py scripts/dev-harness/tests/test_local_launcher.py
	cd backend && env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider tests/unit/test_local_recording_delete.py
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider scripts/dev-harness/tests/test_local_library.py
	node --test web-local/*.test.mjs

.PHONY: docker-bootstrap docker-up docker-dev docker-down docker-status docker-logs docker-doctor test-docker

docker-bootstrap:
	./omiloc --runtime docker bootstrap

docker-up:
	./omiloc --runtime docker up

docker-dev:
	./omiloc --runtime docker dev

docker-down:
	./omiloc --runtime docker down

docker-status:
	./omiloc --runtime docker status

docker-logs:
	./omiloc --runtime docker logs

docker-doctor:
	./omiloc --runtime docker doctor

test-docker:
	env -u PROVIDER_MODE PYTHONDONTWRITEBYTECODE=1 "$${PYTHON}" -m pytest -q -p no:cacheprovider docker/test_runtime.py

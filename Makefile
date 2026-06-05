.PHONY: build-sidecar build-sidecar-arm64 build-sidecar-x86_64 build-app release release-local release-signed

build-sidecar:
	bash scripts/build-sidecar.sh $(ARCH)

build-sidecar-arm64:
	bash scripts/build-sidecar.sh arm64

build-sidecar-x86_64:
	bash scripts/build-sidecar.sh x86_64

build-app:
	$(MAKE) release-local

release:
	@test -n "$(VERSION)" || (echo "set VERSION=x.y.z" && exit 1)
	python scripts/release.py "$(VERSION)"

release-local:
	APPLE_SIGNING_IDENTITY=- pnpm -C app tauri build --bundles app --config '{"bundle":{"createUpdaterArtifacts":false}}'

release-signed:
	@test -n "$$APPLE_SIGNING_IDENTITY" || (echo "set APPLE_SIGNING_IDENTITY" && exit 1)
	pnpm -C app tauri build

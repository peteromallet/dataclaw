"""Direct tests for dataclaw.providers — the provider registry and ModuleProvider."""

from dataclaw.providers import (
    PROVIDERS,
    ModuleProvider,
    Provider,
    get_provider,
    get_provider_non_anon_string_keys,
    iter_providers,
)


class TestProvidersRegistry:
    def test_all_expected_sources_registered(self):
        expected = {"claude", "codex", "cursor", "custom", "gemini", "hermes", "kimi", "openclaw", "opencode"}
        assert expected.issubset(PROVIDERS.keys())

    def test_iter_providers_returns_all(self):
        providers = iter_providers()
        assert len(providers) == len(PROVIDERS)
        assert {p.source for p in providers} == set(PROVIDERS.keys())

    def test_each_provider_is_module_provider_instance(self):
        for provider in PROVIDERS.values():
            assert isinstance(provider, ModuleProvider)
            assert isinstance(provider, Provider)


class TestGetProvider:
    def test_known_source_returns_provider(self):
        provider = get_provider("claude")
        assert provider is not None
        assert provider.source == "claude"

    def test_unknown_source_returns_none(self):
        assert get_provider("nope") is None


class TestNonAnonStringKeys:
    def test_known_source_returns_frozenset(self):
        keys = get_provider_non_anon_string_keys("claude")
        assert isinstance(keys, frozenset)

    def test_unknown_source_returns_empty_frozenset(self):
        assert get_provider_non_anon_string_keys("nope") == frozenset()


class TestProviderInterface:
    def test_provider_has_session_source_returns_bool(self):
        provider = PROVIDERS["claude"]
        assert isinstance(provider.has_session_source(), bool)

    def test_provider_missing_source_message_is_string(self):
        provider = PROVIDERS["claude"]
        assert isinstance(provider.missing_source_message(), str)

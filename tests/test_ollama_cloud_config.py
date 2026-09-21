# Pure logic tests — no database required.
NO_DB = True

from agent.harness import _load_api_keys, normalize_openai_base_url


def test_ollama_native_urls_are_mapped_to_openai_compatibility_urls():
    assert normalize_openai_base_url("https://ollama.com/api") == "https://ollama.com/v1"
    assert normalize_openai_base_url("http://localhost:11434/api/") == "http://localhost:11434/v1"


def test_existing_openai_compatible_urls_are_preserved():
    assert normalize_openai_base_url("https://ollama.com/v1/") == "https://ollama.com/v1"
    assert normalize_openai_base_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"


def test_ollama_native_api_key_is_supported(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY_1", raising=False)
    monkeypatch.delenv("LLM_API_KEY_2", raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")

    assert _load_api_keys() == ["ollama-cloud-key"]

"""Settings resolution that is not obvious from the field defaults."""

from app.config import Settings


def test_openai_api_key_is_used_when_llm_api_key_blank() -> None:
    s = Settings(_env_file=None, llm_api_key="", openai_api_key="sk-openai")
    assert s.llm_api_key.get_secret_value() == "sk-openai"
    assert s.llm_configured


def test_llm_api_key_overrides_openai_api_key() -> None:
    s = Settings(_env_file=None, llm_api_key="sk-gateway", openai_api_key="sk-openai")
    assert s.llm_api_key.get_secret_value() == "sk-gateway"


def test_no_key_means_not_configured() -> None:
    s = Settings(_env_file=None, llm_api_key="", openai_api_key="")
    assert not s.llm_configured

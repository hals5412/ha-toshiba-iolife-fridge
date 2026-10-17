"""Home Assistant 本体を使うテストの共通設定.

実行: python -m pytest tests_ha  (pytest-homeassistant-custom-component が必要。Linux 向け)
tests/ の軽量テストは HA の部品を差し替えるので、こちらとは別に実行する。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """custom_components 配下の統合を読み込めるようにする."""
    return

"""Tests für den Sprech-Normalisierer."""

from __future__ import annotations

import unittest

from voicebridge.speech import to_speech


class NormalizerTests(unittest.TestCase):
    def test_markdown_und_code(self) -> None:
        text = (
            "## Ergebnis\n\n"
            "**Wichtig:** Der Pfad ist `/tmp/test`.\n\n"
            "```bash\nls -la\n```\n\n"
            "- eins\n- zwei\n\n"
            "Mehr: https://example.com/seite\n"
        )

        spoken = to_speech(text)

        self.assertNotIn("```", spoken)
        self.assertNotIn("**", spoken)
        self.assertNotIn("#", spoken)
        self.assertNotIn("`", spoken)
        self.assertNotIn("https://", spoken)
        self.assertNotIn("- eins", spoken)
        self.assertIn("Link im Terminal", spoken)
        self.assertIn("Wichtig:", spoken)
        self.assertIn("eins", spoken)

    def test_kurzer_text_bleibt(self) -> None:
        self.assertEqual(to_speech("Hallo Welt."), "Hallo Welt.")

    def test_absaetze_werden_verbunden(self) -> None:
        self.assertEqual(to_speech("Erster Satz.\n\nZweiter Satz."), "Erster Satz. Zweiter Satz.")

    def test_ueberschrift_wird_zum_satz(self) -> None:
        self.assertEqual(to_speech("# Ergebnis\n\nFertig."), "Ergebnis. Fertig.")

    def test_kuerzung_mit_hinweis(self) -> None:
        spoken = to_speech("Dies ist ein Satz. " * 40, max_chars=60)

        self.assertTrue(spoken.endswith("Details stehen im Terminal."))
        self.assertIn(". Details stehen im Terminal.", spoken)
        self.assertLess(len(spoken), 60 + 40)

    def test_ohne_satzgrenze_wird_am_wort_gekuerzt(self) -> None:
        spoken = to_speech("wort " * 200, max_chars=40)

        self.assertTrue(spoken.endswith("Details stehen im Terminal."))
        self.assertNotIn("wortwort", spoken)


if __name__ == "__main__":
    unittest.main()

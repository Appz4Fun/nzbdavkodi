# SPDX-License-Identifier: GPL-3.0-or-later
"""Language names and release-name aliases, separate from the vendored parser."""

import re

# Code, stable setting suffix, display name, additional release-name aliases.
LANGUAGES = (
    ("ar", "arabic", "Arabic", "ara"),
    ("bn", "bengali", "Bengali", "ben bangla"),
    ("bg", "bulgarian", "Bulgarian", "bul"),
    ("yue", "cantonese", "Cantonese", "cant"),
    ("zh", "chinese", "Chinese", "chi zho mandarin cmn chs cht"),
    ("hr", "croatian", "Croatian", "hrv"),
    ("cs", "czech", "Czech", "cze ces"),
    ("da", "danish", "Danish", "dan"),
    ("nl", "dutch", "Dutch", "dut nld"),
    ("en", "english", "English", "eng"),
    ("et", "estonian", "Estonian", "est"),
    ("fi", "finnish", "Finnish", "fin"),
    ("fr", "french", "French", "fre fra vff vf vfq vostfr"),
    ("de", "german", "German", "ger deu"),
    ("el", "greek", "Greek", "gre ell"),
    ("gu", "gujarati", "Gujarati", "guj"),
    ("he", "hebrew", "Hebrew", "heb"),
    ("hi", "hindi", "Hindi", "hin"),
    ("hu", "hungarian", "Hungarian", "hun"),
    ("id", "indonesian", "Indonesian", "ind"),
    ("it", "italian", "Italian", "ita"),
    ("ja", "japanese", "Japanese", "jpn jap"),
    ("kn", "kannada", "Kannada", "kan"),
    ("ko", "korean", "Korean", "kor"),
    ("lv", "latvian", "Latvian", "lav"),
    ("lt", "lithuanian", "Lithuanian", "lit"),
    ("ms", "malay", "Malay", "may msa"),
    ("ml", "malayalam", "Malayalam", "mal"),
    ("mr", "marathi", "Marathi", "mar"),
    ("no", "norwegian", "Norwegian", "nor nob nno nb nn"),
    ("fa", "persian", "Persian", "per fas farsi"),
    ("pl", "polish", "Polish", "pol"),
    ("pt", "portuguese", "Portuguese", "por"),
    ("pa", "punjabi", "Punjabi", "pan"),
    ("ro", "romanian", "Romanian", "rum ron"),
    ("ru", "russian", "Russian", "rus"),
    ("sr", "serbian", "Serbian", "srp"),
    ("sk", "slovak", "Slovak", "slo slk"),
    ("sl", "slovenian", "Slovenian", "slv"),
    ("es", "spanish", "Spanish", "spa esp latino latin la castellano"),
    ("sv", "swedish", "Swedish", "swe"),
    ("ta", "tamil", "Tamil", "tam"),
    ("te", "telugu", "Telugu", "tel"),
    ("th", "thai", "Thai", "tha"),
    ("tr", "turkish", "Turkish", "tur"),
    ("uk", "ukrainian", "Ukrainian", "ukr"),
    ("ur", "urdu", "Urdu", "urd urdo"),
    ("vi", "vietnamese", "Vietnamese", "vie"),
)

_ALIASES = {
    alias: code
    for code, _setting, name, aliases in LANGUAGES
    for alias in [code, name.lower()] + aliases.split()
}
_TOKEN = re.compile(r"[a-z]+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_RELEASE_MARKER = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,2}(?:e\d{1,3})?|\d{1,2}x\d{2}"
    r"|\d{3,4}x\d{3,4}|\d{3,4}[pi]|[48]k|qhd)(?![a-z0-9])"
)
_GROUP_SUFFIX = re.compile(
    r"-[a-z0-9_-]+(?:\.(?:mkv|mp4|avi|mov|wmv|m4v|mpg|mpeg|ts|m2ts|mts|webm|nzb))?$"
)
# HE and HR are also language codes, but inside these audio tags they mean
# High Efficiency and High Resolution. Preserve separate language declarations.
_AUDIO_TAGS = re.compile(
    r"(?<![a-z0-9])(?:he[. _-]?aac|dts[. _-]?hd[. _-]?hra?)(?![a-z])"
)


def normalize_languages(values):
    """Normalize names, codes, and Latin American Spanish case-insensitively."""
    return list(dict.fromkeys(_ALIASES.get(v.lower(), v.lower()) for v in values))


def title_languages(title, existing):
    """Supplement PTT languages using the release's metadata tokens.

    The year, episode, or resolution marker keeps title words such as No Good
    Deed out of the supplemental scan. Media tags and group suffixes are not
    language declarations, even when they contain a language code.
    """
    text = title.lower()
    marker = _YEAR.search(text) or _RELEASE_MARKER.search(text)
    text = text[marker.end() :] if marker else ""
    text = _AUDIO_TAGS.sub(" ", text)
    text = _GROUP_SUFFIX.sub("", text)
    tokens = _TOKEN.findall(text)
    found = [_ALIASES[token] for token in tokens if token in _ALIASES]
    languages = normalize_languages(existing)
    # PTT confuses Malay with Malayalam. Preserve Malayalam when both occur.
    if "ms" in found and "ml" not in found:
        languages = [code for code in languages if code != "ml"]
    return list(dict.fromkeys(languages + found))


def language_filter_values(languages):
    """Apply requested filter aliases without changing the detected identity."""
    values = normalize_languages(languages)
    # User-requested grouping: Chinese accepts Cantonese and Urdu. Urdu remains
    # its own language here; this is a filtering preference, not classification.
    if any(code in values for code in ("yue", "ur")):
        values.append("zh")
    return values

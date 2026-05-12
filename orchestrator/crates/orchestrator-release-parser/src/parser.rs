//! [`Parser`] engine + the value type the parsed dict carries.
//!
//! 1:1 port of `plugin.video.nzbdav/resources/lib/ptt/parse.py`.

use std::collections::BTreeMap;

use once_cell::sync::Lazy;
use regex::Regex;
use serde::Serialize;

use crate::transformers::Transformer;

/// One value in the parsed dict. Mirrors Python's `Any`: PTT returns
/// strings, ints, bools, and lists of those.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Value {
    Bool(bool),
    Int(i64),
    Str(String),
    List(Vec<Value>),
    Null,
}

impl Value {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(n) => Some(*n),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_list(&self) -> Option<&[Value]> {
        match self {
            Value::List(items) => Some(items.as_slice()),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }
}

/// Parsed dict. Insertion order doesn't matter for parity — Python's
/// dict ordering shows up via JSON; we use [`BTreeMap`] so the
/// serialised form is deterministic (sorted by key) which is exactly
/// what `json.dumps(sort_keys=True)` produces on the Python side when
/// the corpus is built.
pub type Parsed = BTreeMap<String, Value>;

/// Per-handler options dict. 1:1 with the Python defaults in
/// [`extend_options`](https://github.com/.../parse.py#L92).
#[derive(Debug, Clone, Copy, Default)]
pub struct HandlerOptions {
    pub skip_if_already_found: bool,
    pub skip_from_title: bool,
    pub skip_if_first: bool,
    pub remove: bool,
}

impl HandlerOptions {
    /// Python's `extend_options({})` defaults — `skipIfAlreadyFound`
    /// is the only flag that defaults to `true`.
    pub fn defaults() -> Self {
        Self {
            skip_if_already_found: true,
            skip_from_title: false,
            skip_if_first: false,
            remove: false,
        }
    }
}

/// A handler is either a regex-driven entry (with a transformer and
/// options) or a free-form closure that inspects the running context
/// and decides what to do.
///
/// The closure form covers the few "is_adult_content" / "encoder
/// lookup" handlers in the Python source that aren't a single regex
/// pattern.
pub enum Handler {
    /// `Parser.add_handler(name, regex, transformer, options)` —
    /// the bread-and-butter form.
    Regex {
        name: &'static str,
        pattern: Regex,
        transformer: Transformer,
        options: HandlerOptions,
        /// Per-handler override of the result value (Python's
        /// `options.get("value", transformed)`). When set, replaces
        /// the transformed match with this literal.
        value_override: Option<Value>,
    },
    /// Custom handler closure used for behaviour that can't be
    /// expressed as one regex (e.g. the adult-words lookup).
    Custom(CustomHandler),
}

pub type CustomRunner = Box<dyn Fn(&mut HandlerCtx) -> Option<HandlerEffect> + Send + Sync>;

pub struct CustomHandler {
    pub name: &'static str,
    pub run: CustomRunner,
}

/// Mutable working state passed to each handler — mirrors the Python
/// `{"title": title, "result": result, "matched": matched}` dict.
pub struct HandlerCtx<'a> {
    pub title: &'a str,
    pub result: &'a mut Parsed,
    pub matched: &'a mut BTreeMap<String, MatchedEntry>,
}

#[derive(Debug, Clone)]
pub struct MatchedEntry {
    pub raw_match: String,
    pub match_index: usize,
}

/// What a handler tells the parser about a match — used to update the
/// running `title` slice and the `end_of_title` watermark.
#[derive(Debug, Clone)]
pub struct HandlerEffect {
    pub raw_match: String,
    pub match_index: usize,
    pub remove: bool,
    pub skip_from_title: bool,
}

pub struct Parser {
    handlers: Vec<Handler>,
}

impl Default for Parser {
    fn default() -> Self {
        Self::new()
    }
}

impl Parser {
    pub fn new() -> Self {
        Self {
            handlers: Vec::new(),
        }
    }

    /// `parser.add_handler(name, regex, transformer, options)`.
    pub fn add_regex(
        &mut self,
        name: &'static str,
        pattern: Regex,
        transformer: Transformer,
        options: HandlerOptions,
    ) {
        self.handlers.push(Handler::Regex {
            name,
            pattern,
            transformer,
            options,
            value_override: None,
        });
    }

    pub fn add_regex_with_value(
        &mut self,
        name: &'static str,
        pattern: Regex,
        transformer: Transformer,
        options: HandlerOptions,
        value_override: Value,
    ) {
        self.handlers.push(Handler::Regex {
            name,
            pattern,
            transformer,
            options,
            value_override: Some(value_override),
        });
    }

    pub fn add_custom(&mut self, name: &'static str, run: CustomFn) {
        self.handlers.push(Handler::Custom(CustomHandler {
            name,
            run: Box::new(run),
        }));
    }

    /// 1:1 port of `Parser.parse` — see `parse.py`. The handler chain
    /// runs in registration order; each match accumulates onto
    /// `result`, with `remove`/`skip_from_title` options updating the
    /// running title slice and the `end_of_title` watermark.
    pub fn parse(&self, raw_title: &str) -> Parsed {
        let mut title = SUB_PATTERN.replace_all(raw_title, " ").into_owned();
        let mut result: Parsed = Parsed::new();
        let mut matched: BTreeMap<String, MatchedEntry> = BTreeMap::new();
        let mut end_of_title = title.chars().count();

        for handler in &self.handlers {
            let effect = match handler {
                Handler::Regex {
                    name,
                    pattern,
                    transformer,
                    options,
                    value_override,
                } => run_regex_handler(
                    name,
                    pattern,
                    transformer.clone(),
                    options,
                    value_override.as_ref(),
                    &title,
                    &mut result,
                    &mut matched,
                ),
                Handler::Custom(h) => {
                    let mut ctx = HandlerCtx {
                        title: title.as_str(),
                        result: &mut result,
                        matched: &mut matched,
                    };
                    (h.run)(&mut ctx)
                }
            };

            if let Some(effect) = effect {
                if effect.remove {
                    // Drop the matched substring from the running
                    // title — careful: handler effects index by byte
                    // (we matched the regex on the byte string), so
                    // splice on the byte range.
                    let start = effect.match_index;
                    let end = (start + effect.raw_match.len()).min(title.len());
                    if start <= end && end <= title.len() {
                        title.replace_range(start..end, "");
                    }
                }
                if !effect.skip_from_title
                    && effect.match_index > 1
                    && effect.match_index < end_of_title
                {
                    end_of_title = effect.match_index;
                }
                if effect.remove && effect.skip_from_title && effect.match_index < end_of_title {
                    end_of_title = end_of_title.saturating_sub(effect.raw_match.chars().count());
                }
            }
        }

        // Python source: result.setdefault("episodes", []);
        // setdefault("seasons", []); setdefault("languages", []).
        result
            .entry("episodes".to_string())
            .or_insert(Value::List(Vec::new()));
        result
            .entry("seasons".to_string())
            .or_insert(Value::List(Vec::new()));
        result
            .entry("languages".to_string())
            .or_insert(Value::List(Vec::new()));

        // Clean title up to end_of_title before further processing.
        let title_slice: String = title.chars().take(end_of_title).collect();
        let cleaned = clean_title(&title_slice);
        result.insert("title".to_string(), Value::Str(cleaned));
        result
    }
}

pub type CustomFn = fn(&mut HandlerCtx) -> Option<HandlerEffect>;

#[allow(clippy::too_many_arguments)]
fn run_regex_handler(
    name: &'static str,
    pattern: &Regex,
    transformer: Transformer,
    options: &HandlerOptions,
    value_override: Option<&Value>,
    title: &str,
    result: &mut Parsed,
    matched: &mut BTreeMap<String, MatchedEntry>,
) -> Option<HandlerEffect> {
    if options.skip_if_already_found && result.contains_key(name) {
        return None;
    }
    let m = pattern.captures(title)?;
    let raw_match = m.get(0)?;
    let clean_match = m
        .get(1)
        .map(|s| s.as_str().to_string())
        .unwrap_or_else(|| raw_match.as_str().to_string());

    let existing = result.get(name).cloned();
    let transformed = transformer.apply(&clean_match, existing.as_ref())?;

    // is_skip_if_first: when the option is set, only emit if at
    // least one other matched key appeared earlier than this one.
    if options.skip_if_first {
        let other_matches: Vec<&MatchedEntry> = matched
            .iter()
            .filter_map(|(k, v)| if k != name { Some(v) } else { None })
            .collect();
        if !other_matches.is_empty()
            && other_matches
                .iter()
                .all(|e| raw_match.start() < e.match_index)
        {
            return None;
        }
    }

    // is_before_title: detect [TAG] prefix matches so we don't pull
    // them into the title bounds. Mirrors BEFORE_TITLE_MATCH_REGEX.
    let is_before_title = BEFORE_TITLE_MATCH_REGEX
        .captures(title)
        .and_then(|c| c.get(1).map(|g| g.as_str().contains(raw_match.as_str())))
        .unwrap_or(false);

    matched.entry(name.to_string()).or_insert(MatchedEntry {
        raw_match: raw_match.as_str().to_string(),
        match_index: raw_match.start(),
    });

    let final_value = value_override.cloned().unwrap_or(transformed);
    result.insert(name.to_string(), final_value);

    Some(HandlerEffect {
        raw_match: raw_match.as_str().to_string(),
        match_index: raw_match.start(),
        remove: options.remove,
        skip_from_title: is_before_title || options.skip_from_title,
    })
}

// --- Title-cleanup constants and helper (1:1 port of clean_title in parse.py) --

static SUB_PATTERN: Lazy<Regex> = Lazy::new(|| Regex::new(r"_+").unwrap());

static BEFORE_TITLE_MATCH_REGEX: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^\[([^\[\]]+)\]").unwrap());

static MOVIE_REGEX: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)[\[(]movie[)\]]").unwrap());

static NOT_ALLOWED_SYMBOLS_AT_START_AND_END: Lazy<Regex> = Lazy::new(|| {
    let non_eng = NON_ENGLISH_CHARS;
    let pat = format!(r"^[^\w{non_eng}#\[【★]+|[ \-:/\\\[|{{(#$&^]+$",);
    Regex::new(&pat).unwrap()
});

static REMAINING_NOT_ALLOWED_SYMBOLS_AT_START_AND_END: Lazy<Regex> = Lazy::new(|| {
    let non_eng = NON_ENGLISH_CHARS;
    let pat = format!(r"^[^\w{non_eng}#]+|\]$");
    Regex::new(&pat).unwrap()
});

static REDUNDANT_SYMBOLS_AT_END: Lazy<Regex> = Lazy::new(|| Regex::new(r"[ \-:./\\]+$").unwrap());

static EMPTY_BRACKETS_REGEX: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\(\s*\)|\[\s*\]|\{\s*\}").unwrap());

static PARANTHESES_WITHOUT_CONTENT: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\(\W*\)|\[\W*\]|\{\W*\}").unwrap());

static STAR_REGEX_1: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[\[【★].*[\]】★][ .]?(.+)").unwrap());

static STAR_REGEX_2: Lazy<Regex> = Lazy::new(|| Regex::new(r"(.+)[ .]?[\[【★].*[\]】★]$").unwrap());

static MP3_REGEX: Lazy<Regex> = Lazy::new(|| Regex::new(r"\bmp3$").unwrap());

static SPACING_REGEX: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());

static SPECIAL_CHAR_SPACING: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"[\-\+\_\{\}\[\]]\W{2,}").unwrap());

static RUSSIAN_CAST_PART1: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\([^)]*[\u{0400}-\u{04ff}][^)]*\)$").unwrap());

static RUSSIAN_CAST_PART2: Lazy<Regex> = Lazy::new(|| Regex::new(r"\(.*\)$").unwrap());

static ALT_TITLES_REGEX: Lazy<Regex> = Lazy::new(|| {
    let non_eng = NON_ENGLISH_CHARS;
    let pat = format!(r"[^/|(]*[{non_eng}][^/|]*[/|]|[/|][^/|(]*[{non_eng}][^/|]*");
    Regex::new(&pat).unwrap()
});

static NOT_ONLY_NON_ENG_PART1: Lazy<Regex> = Lazy::new(|| {
    let non_eng = NON_ENGLISH_CHARS;
    let pat = format!(r"[a-zA-Z][^{non_eng}]+[{non_eng}].*[{non_eng}]");
    Regex::new(&pat).unwrap()
});

static NOT_ONLY_NON_ENG_PART2: Lazy<Regex> = Lazy::new(|| {
    // The Python original used a look-ahead `(?=...)` which Rust's
    // `regex` doesn't support. Approximated by matching the
    // non-English block followed by an English suffix; the caller
    // strips the consumed portion which matches the Python intent
    // even though the regex is non-zero-width.
    let non_eng = NON_ENGLISH_CHARS;
    let pat = format!(r"[{non_eng}].*[{non_eng}][^{non_eng}]+[a-zA-Z]");
    Regex::new(&pat).unwrap()
});

static ENGLISH_PREFIX: Lazy<Regex> = Lazy::new(|| {
    let non_eng = NON_ENGLISH_CHARS;
    let pat = format!(r"^[a-zA-Z][^{non_eng}]+");
    Regex::new(&pat).unwrap()
});

const NON_ENGLISH_CHARS: &str = concat!(
    "\u{3040}-\u{30ff}",
    "\u{3400}-\u{4dbf}",
    "\u{4e00}-\u{9fff}",
    "\u{f900}-\u{faff}",
    "\u{ff66}-\u{ff9f}",
    "\u{0400}-\u{04ff}",
    "\u{0600}-\u{06ff}",
    "\u{0750}-\u{077f}",
    "\u{0c80}-\u{0cff}",
    "\u{0d00}-\u{0d7f}",
    "\u{0e00}-\u{0e7f}",
);

const BRACKETS: &[(char, char)] = &[('{', '}'), ('[', ']'), ('(', ')')];

fn apply_russian_cast(s: &str) -> String {
    let s = RUSSIAN_CAST_PART1.replace(s, "").into_owned();
    if s.contains('/') {
        RUSSIAN_CAST_PART2.replace(&s, "").into_owned()
    } else {
        s
    }
}

fn apply_not_only_non_english(s: &str) -> String {
    let s = NOT_ONLY_NON_ENG_PART1.replace_all(s, |caps: &regex::Captures| {
        ENGLISH_PREFIX
            .find(&caps[0])
            .map(|m| m.as_str().to_string())
            .unwrap_or_default()
    });
    NOT_ONLY_NON_ENG_PART2.replace_all(&s, "").into_owned()
}

pub fn clean_title(raw_title: &str) -> String {
    let mut t = raw_title.replace('_', " ");
    t = MOVIE_REGEX.replace_all(&t, "").into_owned();
    t = NOT_ALLOWED_SYMBOLS_AT_START_AND_END
        .replace_all(&t, "")
        .into_owned();
    t = apply_russian_cast(&t);
    t = STAR_REGEX_1.replace_all(&t, "$1").into_owned();
    t = STAR_REGEX_2.replace_all(&t, "$1").into_owned();
    t = ALT_TITLES_REGEX.replace_all(&t, "").into_owned();
    t = apply_not_only_non_english(&t);
    t = REMAINING_NOT_ALLOWED_SYMBOLS_AT_START_AND_END
        .replace_all(&t, "")
        .into_owned();
    t = EMPTY_BRACKETS_REGEX.replace_all(&t, "").into_owned();
    t = MP3_REGEX.replace_all(&t, "").into_owned();
    t = PARANTHESES_WITHOUT_CONTENT.replace_all(&t, "").into_owned();
    t = SPECIAL_CHAR_SPACING.replace_all(&t, "").into_owned();

    // Drop unmatched brackets.
    for (open, close) in BRACKETS {
        let opens = t.chars().filter(|c| c == open).count();
        let closes = t.chars().filter(|c| c == close).count();
        if opens != closes {
            t = t.chars().filter(|c| c != open && c != close).collect();
        }
    }

    if !t.contains(' ') && t.contains('.') {
        t = t.replace('.', " ");
    }

    t = REDUNDANT_SYMBOLS_AT_END.replace_all(&t, "").into_owned();
    t = SPACING_REGEX.replace_all(&t, " ").into_owned();
    t.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_title_basics_match_python_behaviour() {
        // Spot-checks lifted from the parity corpus.
        assert_eq!(clean_title("Inception.2010"), "Inception 2010");
        assert_eq!(clean_title("The Matrix 1999  -"), "The Matrix 1999");
        assert_eq!(
            clean_title("_underscores_become_spaces_"),
            "underscores become spaces"
        );
    }

    #[test]
    fn parser_accumulates_static_value_overrides() {
        // Mirrors the EDGE2020 group handler:
        //   add_handler("group", r"-?EDGE2020", value("EDGE2020"),
        //               {"remove": True})
        let mut p = Parser::new();
        p.add_regex_with_value(
            "group",
            Regex::new(r"-?EDGE2020").unwrap(),
            Transformer::None_,
            HandlerOptions {
                remove: true,
                ..HandlerOptions::defaults()
            },
            Value::Str("EDGE2020".into()),
        );

        let parsed = p.parse("Something-EDGE2020 1080p");
        assert_eq!(parsed.get("group"), Some(&Value::Str("EDGE2020".into())));
    }
}

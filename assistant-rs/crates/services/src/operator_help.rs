//! Bounded, pure retrieval from the release-bundled operator-help corpus.
//! Input is already hydrated operator text. No knowledge database, provider,
//! workspace, credential, network or execution capability is available here.
use crate::execution_context::estimate_text_tokens;
use aho_corasick::AhoCorasick;
use num_bigint::BigInt;
use num_traits::One;
use serde::Serialize;
use serde_json::Number;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeSet, HashMap},
    sync::LazyLock,
};

pub const CORPUS_ID: &str = "nebula.operator-help/v1";
pub const CORPUS_SHA256: &str = "b08e4639c7ead4144487d75de7b39a14577cb5d631100668b82ef907484be7f6";
pub const MAX_QUERY_BYTES: usize = 1024 * 1024;
pub const MAX_QUERY_COUNT: usize = 64;
pub const MAX_OUTPUT_BYTES: usize = 128 * 1024;
const MAX_FOLDED_BYTES: usize = 3 * MAX_QUERY_BYTES;
const MAX_TERMS: usize = 10_000;
const MAX_INTEGER_DIGITS: usize = 4300;
const CORPUS: &str = include_str!("operator_help/corpus.md");
const STOP: &[&str] = &[
    "about", "after", "and", "are", "can", "does", "for", "from", "how", "into", "nebula",
    "operator", "that", "the", "this", "use", "what", "when", "with",
];
const PRODUCT: &[&str] = &[
    "approval",
    "artifact",
    "assistant",
    "automate",
    "browser",
    "compaction",
    "core",
    "desktop",
    "docker",
    "doctor",
    "export",
    "image",
    "import",
    "migration",
    "model",
    "nebula",
    "podman",
    "provider",
    "runner",
    "sandbox",
    "scope",
    "sidecar",
    "terminal",
    "runtime",
    "workspace",
];
const FAILURE: &[&str] = &[
    "blocked",
    "cancelled",
    "corrupt",
    "denied",
    "disabled",
    "error",
    "exit_code",
    "failed",
    "failure",
    "missing",
    "offline",
    "rejected",
    "stopped",
    "timed",
    "timed_out",
    "timeout",
    "unavailable",
    "unhealthy",
];
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum Error {
    #[error("packaged operator help exceeds the preparation capacity bound")]
    Capacity,
    #[error("packaged operator help requires an integer token budget")]
    InvalidBudget,
    #[error("packaged operator-help corpus could not be verified")]
    Corpus,
}
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Serialize)]
pub struct OperatorHelpArticle {
    pub article_id: String,
    pub title: String,
    pub keywords: Vec<String>,
    pub sources: Vec<String>,
    pub body: String,
    pub source_id: String,
    pub chunk_id: String,
    pub reference_text: String,
    pub estimated_tokens: usize,
}
impl std::fmt::Debug for OperatorHelpArticle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OperatorHelpArticle")
            .field("article_id", &self.article_id)
            .finish_non_exhaustive()
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct HelpMatch {
    pub article_id: String,
    pub score: u64,
}
#[derive(Clone, Eq, PartialEq, Serialize)]
pub struct HelpCitation {
    pub source_id: String,
    pub name: String,
    pub citation: Option<String>,
    pub artifact_id: Option<String>,
    pub chunk_id: String,
    pub page: Option<u64>,
    pub excerpt: String,
}
#[derive(Serialize)]
pub struct HelpChunk {
    pub citation: HelpCitation,
    pub text: String,
    pub local_only: bool,
    pub score: u64,
    pub ordinal: usize,
}
#[derive(Serialize)]
pub struct OperatorHelpProjection {
    pub chunks: Vec<HelpChunk>,
    pub citations: Vec<HelpCitation>,
    pub instruction_suffix: String,
    pub estimated_tokens: usize,
}
impl std::fmt::Debug for OperatorHelpProjection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OperatorHelpProjection")
            .field("chunks", &self.chunks.len())
            .field("estimated_tokens", &self.estimated_tokens)
            .finish_non_exhaustive()
    }
}
struct Count(usize);
impl std::io::Write for Count {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_OUTPUT_BYTES {
            return Err(std::io::Error::other("help output byte bound"));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn bound_output<T: Serialize + ?Sized>(value: &T) -> Result<()> {
    serde_json::to_writer(Count(0), value).map_err(|_| Error::Capacity)
}
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
fn trim(value: &str) -> &str {
    value.trim_matches(py_space)
}
fn folded(value: &str, output: &mut String) -> Result<()> {
    for c in value.chars() {
        match crate::unicode_casefold::MAPPINGS.binary_search_by_key(&c, |(key, _)| *key) {
            Ok(index) => output.push_str(crate::unicode_casefold::MAPPINGS[index].1),
            Err(_) => output.push(c),
        }
        if output.len() > MAX_FOLDED_BYTES {
            return Err(Error::Capacity);
        }
    }
    Ok(())
}
fn fold(value: &str) -> Result<String> {
    let mut output = String::with_capacity(value.len());
    folded(value, &mut output)?;
    Ok(output)
}
fn query_text(queries: &[String]) -> Result<String> {
    if queries.len() > MAX_QUERY_COUNT {
        return Err(Error::Capacity);
    }
    let bytes = queries
        .iter()
        .try_fold(queries.len().saturating_sub(1), |total, text| {
            total.checked_add(text.len())
        })
        .ok_or(Error::Capacity)?;
    if bytes > MAX_QUERY_BYTES {
        return Err(Error::Capacity);
    }
    let mut output = String::with_capacity(bytes);
    for (index, query) in queries.iter().enumerate() {
        if index != 0 {
            output.push(' ');
        }
        folded(query, &mut output)?;
    }
    Ok(output)
}
fn word_continue(b: u8) -> bool {
    b.is_ascii_lowercase() || b.is_ascii_digit() || b"_.:/-".contains(&b)
}
fn terms(text: &str) -> Result<BTreeSet<&str>> {
    let bytes = text.as_bytes();
    let mut index = 0;
    let mut output = BTreeSet::new();
    while index < bytes.len() {
        if !bytes[index].is_ascii_lowercase() && !bytes[index].is_ascii_digit() {
            index += 1;
            continue;
        }
        let start = index;
        index += 1;
        while index < bytes.len() && word_continue(bytes[index]) {
            index += 1;
        }
        if index - start >= 3 {
            output.insert(&text[start..index]);
            if output.len() > MAX_TERMS {
                return Err(Error::Capacity);
            }
        }
    }
    Ok(output)
}
// Index positions once for the immutable corpus, rather than rescanning every
// full article for each distinct query term. All source terms are >=3 ASCII
// bytes; their starting positions can never split a UTF-8 scalar.
struct IndexedText {
    text: String,
    positions: HashMap<[u8; 3], Vec<usize>>,
}
impl IndexedText {
    fn new(text: String) -> Self {
        let mut positions: HashMap<[u8; 3], Vec<usize>> = HashMap::new();
        for (index, chunk) in text.as_bytes().windows(3).enumerate() {
            if chunk.iter().all(u8::is_ascii) {
                positions
                    .entry([chunk[0], chunk[1], chunk[2]])
                    .or_default()
                    .push(index);
            }
        }
        Self { text, positions }
    }
    fn count(&self, needle: &str, cap: usize) -> usize {
        if needle.len() < 3 || cap == 0 {
            return 0;
        }
        let prefix = needle.as_bytes();
        let Some(positions) = self.positions.get(&[prefix[0], prefix[1], prefix[2]]) else {
            return 0;
        };
        let mut end = 0;
        let mut count = 0;
        for &position in positions {
            if position >= end && self.text[position..].starts_with(needle) {
                count += 1;
                end = position + needle.len();
                if count == cap {
                    break;
                }
            }
        }
        count
    }
    fn contains(&self, needle: &str) -> bool {
        self.count(needle, 1) != 0
    }
}
struct IndexedArticle {
    searchable: IndexedText,
    title: IndexedText,
    keywords: IndexedText,
    keyword_ids: Vec<usize>,
}
struct Corpus {
    articles: Vec<OperatorHelpArticle>,
    index: Vec<IndexedArticle>,
    keywords: AhoCorasick,
    keyword_count: usize,
}
static INDEX: LazyLock<Result<Corpus>> = LazyLock::new(load_corpus);
fn corpus() -> Result<&'static Corpus> {
    INDEX.as_ref().map_err(|error| *error)
}
fn load_corpus() -> Result<Corpus> {
    if CORPUS.len() > 64 * 1024
        || format!("{:x}", Sha256::digest(CORPUS.as_bytes())) != CORPUS_SHA256
        || !CORPUS.contains(&format!("Corpus: `{CORPUS_ID}`"))
    {
        return Err(Error::Corpus);
    }
    let mut articles = Vec::new();
    let mut identifiers = BTreeSet::new();
    let mut index = Vec::new();
    let mut all_keywords = Vec::<String>::new();
    let mut keyword_map = HashMap::new();
    for block in CORPUS.split("\n## ").skip(1) {
        if articles.len() == 64 {
            return Err(Error::Corpus);
        }
        let (header, remainder) = block.split_once("\n\n").ok_or(Error::Corpus)?;
        let (article_id, title) = trim(header).split_once(" | ").ok_or(Error::Corpus)?;
        if article_id.is_empty()
            || !article_id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            || title.is_empty()
            || title.contains('\n')
            || !identifiers.insert(article_id.to_owned())
        {
            return Err(Error::Corpus);
        }
        let (keyword_line, tail) = remainder.split_once("\n\n").ok_or(Error::Corpus)?;
        let (source_line, body) = tail.split_once("\n\n").ok_or(Error::Corpus)?;
        let keywords: Vec<String> = keyword_line
            .strip_prefix("Keywords:")
            .ok_or(Error::Corpus)?
            .split(',')
            .map(|s| trim(s).to_owned())
            .collect();
        let sources: Vec<String> = source_line
            .strip_prefix("Sources:")
            .ok_or(Error::Corpus)?
            .split(',')
            .map(|s| trim(s).to_owned())
            .collect();
        let title = trim(title).to_owned();
        let body = trim(body).to_owned();
        let reference_text = format!(
            "{title}\n\n{body}\n\nImplementation references: {}",
            sources.join(", ")
        );
        let article = OperatorHelpArticle {
            article_id: article_id.into(),
            title,
            keywords,
            sources,
            source_id: format!("nebula-help:{article_id}"),
            chunk_id: format!(
                "{article_id}:{:.16}",
                format!("{:x}", Sha256::digest(body.as_bytes()))
            ),
            body,
            estimated_tokens: estimate_text_tokens(&reference_text, 1)
                .map_err(|_| Error::Corpus)?,
            reference_text,
        };
        let title_text = fold(&article.title)?;
        let keyword_text = fold(&article.keywords.join(" "))?;
        let searchable = format!("{title_text} {keyword_text} {}", fold(&article.body)?);
        let mut keyword_ids = Vec::new();
        for keyword in &article.keywords {
            let text = fold(keyword)?;
            // The embedded corpus has no empty keywords. Treat an unexpected
            // change as a packaging error instead of creating a match-all rule.
            if text.is_empty() {
                return Err(Error::Corpus);
            }
            let id = if let Some(id) = keyword_map.get(&text) {
                *id
            } else {
                let id = all_keywords.len();
                all_keywords.push(text.clone());
                keyword_map.insert(text, id);
                id
            };
            keyword_ids.push(id);
        }
        index.push(IndexedArticle {
            searchable: IndexedText::new(searchable),
            title: IndexedText::new(title_text),
            keywords: IndexedText::new(keyword_text),
            keyword_ids,
        });
        articles.push(article);
    }
    if articles.is_empty() {
        return Err(Error::Corpus);
    }
    let keywords = AhoCorasick::new(&all_keywords).map_err(|_| Error::Corpus)?;
    Ok(Corpus {
        articles,
        index,
        keywords,
        keyword_count: all_keywords.len(),
    })
}
pub fn operator_help_articles() -> Result<&'static [OperatorHelpArticle]> {
    Ok(&corpus()?.articles)
}
fn ranked(queries: &[String], limit: usize) -> Result<Vec<(usize, u64)>> {
    if limit == 0 {
        return Ok(vec![]);
    }
    let text = query_text(queries)?;
    let raw_terms = terms(&text)?;
    if !raw_terms
        .iter()
        .any(|term| PRODUCT.contains(term) || FAILURE.contains(term))
    {
        return Ok(vec![]);
    }
    let terms: Vec<_> = raw_terms
        .into_iter()
        .filter(|term| !STOP.contains(term) && !FAILURE.contains(term))
        .collect();
    let corpus = corpus()?;
    let mut keyword_seen = vec![false; corpus.keyword_count];
    // Overlap is required: e.g. 'mcp', 'mcp server' and 'mcp server description'
    // must all receive their own boost, including identical keywords shared by
    // different articles. Repetition of one keyword never adds a second boost.
    for found in corpus.keywords.find_overlapping_iter(text.as_bytes()) {
        keyword_seen[found.pattern().as_usize()] = true;
    }
    let mut ranked = Vec::with_capacity(corpus.articles.len());
    for (ordinal, article) in corpus.index.iter().enumerate() {
        let mut score = 0u64;
        for term in &terms {
            score += (article.searchable.count(term, 3) * 2) as u64;
            if article.title.contains(term) || article.keywords.contains(term) {
                score += 3;
            }
        }
        score += 10
            * article
                .keyword_ids
                .iter()
                .filter(|&&id| keyword_seen[id])
                .count() as u64;
        if score >= 6 {
            ranked.push((ordinal, score));
        }
    }
    ranked.sort_by(|left, right| right.1.cmp(&left.1).then(left.0.cmp(&right.0)));
    ranked.truncate(limit);
    Ok(ranked)
}
pub fn search_operator_help(queries: &[String], limit: usize) -> Result<Vec<HelpMatch>> {
    let ranked = ranked(queries, limit)?;
    if ranked.is_empty() {
        return Ok(vec![]);
    }
    let corpus = corpus()?;
    let output: Vec<_> = ranked
        .into_iter()
        .map(|(index, score)| HelpMatch {
            article_id: corpus.articles[index].article_id.clone(),
            score,
        })
        .collect();
    bound_output(&output)?;
    Ok(output)
}
fn integer(value: &Number) -> Result<BigInt> {
    let text = value.to_string();
    if text.len() > MAX_INTEGER_DIGITS + usize::from(text.starts_with('-')) {
        return Err(Error::Capacity);
    }
    if text.bytes().any(|b| b == b'.' || b == b'e' || b == b'E') {
        return Err(Error::InvalidBudget);
    }
    BigInt::parse_bytes(text.as_bytes(), 10).ok_or(Error::InvalidBudget)
}
/// Source max(1, target_input_tokens // 5), without narrowing arbitrary integers.
pub fn operator_help_budget(target_input_tokens: &Number) -> Result<Number> {
    let value = (integer(target_input_tokens)? / 5u8).max(BigInt::one());
    value.to_string().parse().map_err(|_| Error::InvalidBudget)
}
#[derive(Serialize)]
struct Reference<'a> {
    source_id: &'a str,
    chunk_id: &'a str,
    name: &'a str,
    citation: &'a Option<String>,
    text: &'a str,
}
pub fn prepare_operator_help(
    queries: &[String],
    token_budget: &Number,
) -> Result<OperatorHelpProjection> {
    let budget = integer(token_budget)?;
    let matches = ranked(queries, 8)?;
    let mut output = OperatorHelpProjection {
        chunks: vec![],
        citations: vec![],
        instruction_suffix: String::new(),
        estimated_tokens: 0,
    };
    if matches.is_empty() {
        return Ok(output);
    }
    let corpus = corpus()?;
    for (ordinal, (index, score)) in matches.into_iter().enumerate() {
        let article = &corpus.articles[index];
        if BigInt::from(output.estimated_tokens + article.estimated_tokens) > budget {
            continue;
        }
        let collapsed = article
            .body
            .split(py_space)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        let excerpt: String = collapsed.chars().take(320).collect();
        // ChatCitation applies Pydantic str_strip_whitespace after truncation.
        let citation = HelpCitation {
            source_id: article.source_id.clone(),
            name: article.title.clone(),
            citation: Some(format!("{CORPUS_ID} / {}", article.article_id)),
            artifact_id: None,
            chunk_id: article.chunk_id.clone(),
            page: None,
            excerpt: excerpt.trim().into(),
        };
        output.citations.push(citation.clone());
        output.chunks.push(HelpChunk {
            citation,
            text: article.reference_text.clone(),
            local_only: false,
            score,
            ordinal,
        });
        output.estimated_tokens += article.estimated_tokens;
    }
    if !output.chunks.is_empty() {
        let reference: Vec<_> = output
            .chunks
            .iter()
            .map(|chunk| Reference {
                source_id: &chunk.citation.source_id,
                chunk_id: &chunk.citation.chunk_id,
                name: &chunk.citation.name,
                citation: &chunk.citation.citation,
                text: &chunk.text,
            })
            .collect();
        let data = serde_json::to_string(&reference).map_err(|_| Error::Corpus)?;
        output.instruction_suffix = format!(
            "\n\nBEGIN NEBULA OPERATOR HELP (JSON)\n{data}\nEND NEBULA OPERATOR HELP\nIf no help article matches an observed Nebula failure, report the exact error and say that no verified recovery procedure is available."
        );
    }
    bound_output(&output)?;
    Ok(output)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn substring_counts_follow_python_nonoverlap() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../compatibility/python-operator-help.json"
        ))
        .unwrap();
        for case in fixture["count_vectors"].as_array().unwrap() {
            let indexed = IndexedText::new(case["text"].as_str().unwrap().into());
            assert_eq!(
                indexed.count(case["needle"].as_str().unwrap(), 3),
                case["expected"].as_u64().unwrap() as usize
            );
        }
    }
}

//! FastAPI's body location prefix without copying diagnostic input per issue.
use nebula_assistant_domain::model_validation::{Location, ValidationIssueRef, ValidationReport};
use serde::{
    Serialize, Serializer,
    ser::{SerializeSeq, SerializeStruct},
};

pub(super) struct RequestReport(pub Box<ValidationReport>);
struct BodyLocation<'a>(&'a [Location]);
impl Serialize for BodyLocation<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.0.len() + 1))?;
        seq.serialize_element("body")?;
        for part in self.0 {
            seq.serialize_element(part)?;
        }
        seq.end()
    }
}
struct RequestIssue<'a>(ValidationIssueRef<'a>);
impl Serialize for RequestIssue<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let issue = &self.0;
        let mut row =
            serializer.serialize_struct("RequestIssue", 4 + usize::from(issue.ctx.is_some()))?;
        row.serialize_field("type", issue.kind)?;
        row.serialize_field("loc", &BodyLocation(issue.loc))?;
        row.serialize_field("msg", issue.msg)?;
        row.serialize_field("input", issue.input)?;
        if let Some(ctx) = issue.ctx {
            row.serialize_field("ctx", ctx)?;
        }
        row.end()
    }
}
impl Serialize for RequestReport {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.0.len()))?;
        for issue in self.0.issues() {
            seq.serialize_element(&RequestIssue(issue))?;
        }
        seq.end()
    }
}

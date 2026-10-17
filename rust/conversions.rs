//! Owned conversions not expressible through the current projected From/Cow surfaces.
//! No routing, HTTP policy, source inspection, or request extraction belongs here.

pub fn body_from_text(text: String) -> axum_core::body::Body {
    axum_core::body::Body::from(text)
}

pub fn buffer_into_text(buffer: axum::body::Bytes) -> Option<String> {
    String::from_utf8(buffer.into()).ok()
}

pub fn decode_path_component(value: String) -> Option<String> {
    match urlencoding::decode(&value) {
        Ok(std::borrow::Cow::Borrowed(_)) => Some(value),
        Ok(std::borrow::Cow::Owned(decoded)) => Some(decoded),
        Err(_) => None,
    }
}

/// A metadata-only HEAD response has no known body length. Empty's exact zero size hint
/// would make the router invent Content-Length: 0; this body intentionally has no hint.
pub fn metadata_only_body() -> axum_core::body::Body {
    struct MetadataOnly;
    impl http_body::Body for MetadataOnly {
        type Data = axum::body::Bytes;
        type Error = std::convert::Infallible;
        fn poll_frame(
            self: std::pin::Pin<&mut Self>,
            _: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Result<http_body::Frame<Self::Data>, Self::Error>>> {
            std::task::Poll::Ready(None)
        }
    }
    axum_core::body::Body::new(MetadataOnly)
}

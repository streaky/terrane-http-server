//! Streaming multipart decoding and private temporary-file staging.
//!
//! Rust owns the boundary to Multer's streaming field parser and writes through
//! the descriptor returned by the core temporary-file implementation. Remove
//! this bridge once the field stream and its asynchronous file sink are
//! expressible in Terrane. Request policy and surfaced errors stay in Terrane.

use axum::body::{Body, Bytes};
use futures_util::{Stream, StreamExt};
use http_body::Body as _;
use multer::{Constraints, Error as MultipartError, Multipart, SizeLimit};
use std::{
    pin::Pin,
    task::{Context, Poll},
    time::Duration,
};
use terrane_platform_support::temporary::TemporaryPath;
use tokio::io::AsyncWriteExt;

pub struct StagedPart {
    pub name: String,
    pub value: Option<String>,
    pub filename: Option<String>,
    pub media_type: Option<String>,
    pub size: u64,
    pub temporary: Option<(TemporaryPath, TemporaryPath)>,
}

pub struct StagedForm {
    pub parts: Vec<StagedPart>,
    pub bytes_read: u64,
}

pub enum StageError {
    Timeout,
    Limit,
    Storage,
    Malformed,
    Unsupported,
}
fn map_multer_error(error: MultipartError) -> StageError {
    match error {
        MultipartError::FieldSizeExceeded { .. } | MultipartError::StreamSizeExceeded { .. } => {
            StageError::Limit
        }
        _ => StageError::Malformed,
    }
}

struct MeteredBody {
    body: Body,
    read: u64,
}

impl Stream for MeteredBody {
    type Item = Result<Bytes, axum::Error>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        loop {
            match Pin::new(&mut self.body).poll_frame(cx) {
                Poll::Ready(Some(Ok(frame))) => match frame.into_data() {
                    Ok(bytes) => {
                        self.read = self.read.saturating_add(bytes.len() as u64);
                        return Poll::Ready(Some(Ok(bytes)));
                    }
                    Err(_) => continue,
                },
                Poll::Ready(Some(Err(error))) => return Poll::Ready(Some(Err(error))),
                Poll::Ready(None) => return Poll::Ready(None),
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

async fn stage_inner(
    body: Body,
    content_type: &str,
    maximum: u64,
    maximum_parts: u32,
) -> Result<StagedForm, StageError> {
    let boundary = multer::parse_boundary(content_type).map_err(|error| match error {
        MultipartError::NoMultipart => StageError::Unsupported,
        _ => StageError::Malformed,
    })?;
    let mut meter = MeteredBody { body, read: 0 };
    let constraints = Constraints::new().size_limit(SizeLimit::new().whole_stream(maximum));
    let mut multipart = Multipart::with_constraints(&mut meter, boundary, constraints);
    let mut parts = Vec::new();
    while let Some(field) = multipart.next_field().await.map_err(map_multer_error)? {
        if parts.len() >= maximum_parts as usize {
            return Err(StageError::Limit);
        }
        let name = field.name().unwrap_or("").to_owned();
        let filename = field.file_name().map(str::to_owned);
        let media_type = field.content_type().map(|value| value.to_string());
        if filename.is_none() {
            let value = String::from_utf8(field.bytes().await.map_err(map_multer_error)?.into())
                .map_err(|_| StageError::Malformed)?;
            parts.push(StagedPart {
                name,
                value: Some(value),
                filename: None,
                media_type,
                size: 0,
                temporary: None,
            });
            continue;
        }
        // Each upload gets a private, exclusively-created directory. Client filenames
        // are metadata only; the random server-generated name is used on disk.
        let directory = TemporaryPath::directory(None).map_err(|_| StageError::Storage)?;
        let (temporary, native_file) =
            TemporaryPath::file(Some(directory.path())).map_err(|_| StageError::Storage)?;
        let mut file = tokio::fs::File::from_std(native_file);
        let mut size = 0u64;
        let mut field = field;
        while let Some(chunk) = field.chunk().await.map_err(map_multer_error)? {
            size = size.saturating_add(chunk.len() as u64);
            file.write_all(&chunk)
                .await
                .map_err(|_| StageError::Storage)?;
        }
        file.flush().await.map_err(|_| StageError::Storage)?;
        drop(file);
        parts.push(StagedPart {
            name,
            value: None,
            filename,
            media_type,
            size,
            temporary: Some((temporary, directory)),
        });
    }
    // A closing delimiter can precede an epilogue. Count all wire bytes and
    // observe a disconnect before exposing any staged files to the handler.
    drop(multipart);
    while let Some(chunk) = meter.next().await {
        chunk.map_err(|_| StageError::Malformed)?;
        if meter.read > maximum {
            return Err(StageError::Limit);
        }
    }
    Ok(StagedForm {
        parts,
        bytes_read: meter.read,
    })
}

pub async fn stage(
    body: Body,
    content_type: &str,
    maximum: u64,
    maximum_parts: u32,
    timeout_ms: u64,
) -> Result<StagedForm, StageError> {
    let operation = stage_inner(body, content_type, maximum, maximum_parts);
    if timeout_ms == 0 {
        operation.await
    } else {
        tokio::time::timeout(Duration::from_millis(timeout_ms), operation)
            .await
            .map_err(|_| StageError::Timeout)?
    }
}

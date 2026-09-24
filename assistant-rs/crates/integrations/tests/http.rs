use nebula_assistant_integrations::*;
use serde_json::json;
use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::mpsc,
};
fn request() -> ModelRequest {
    ModelRequest::text(
        "fixture",
        vec![ModelMessage {
            role: "user".into(),
            content: json!("Hello"),
        }],
    )
}
fn pool(limits: PoolLimits) -> Arc<ProviderPool> {
    Arc::new(
        ProviderPool::new(PoolLimits {
            trust_env: false,
            ..limits
        })
        .unwrap(),
    )
}
fn credential_provider(
    url: String,
    pool: Arc<ProviderPool>,
    attempts: u8,
    credential: &str,
) -> OpenAiProvider {
    let mut c = ProviderConfig::new("fixture", url)
        .with_headers([("authorization".into(), format!("Bearer {credential}"))])
        .unwrap();
    c.retry_attempts = attempts;
    c.retry_backoff = Duration::ZERO;
    c.timeout = Duration::from_secs(2);
    c.completion_timeout = Duration::from_secs(2);
    OpenAiProvider::new(c, pool).unwrap()
}
fn provider(url: String, pool: Arc<ProviderPool>, attempts: u8) -> OpenAiProvider {
    credential_provider(url, pool, attempts, "fixture-do-not-print")
}
async fn read_request(socket: &mut TcpStream) -> Vec<u8> {
    let mut raw = vec![];
    loop {
        let mut part = [0u8; 4096];
        let n = socket.read(&mut part).await.unwrap();
        assert!(n > 0);
        raw.extend_from_slice(&part[..n]);
        assert!(raw.len() < 64 * 1024);
        if let Some(i) = raw.windows(4).position(|v| v == b"\r\n\r\n") {
            let head = std::str::from_utf8(&raw[..i]).unwrap();
            let length = head
                .lines()
                .find_map(|l| {
                    l.to_lowercase()
                        .strip_prefix("content-length:")
                        .and_then(|s| s.trim().parse::<usize>().ok())
                })
                .unwrap_or(0);
            if raw.len() >= i + 4 + length {
                return raw;
            }
        }
    }
}
async fn respond_bytes(
    socket: &mut TcpStream,
    status: &str,
    headers: &str,
    body: &[u8],
    keep_alive: bool,
) {
    let header = format!(
        "HTTP/1.1 {status}\r\n{headers}Content-Length: {}\r\nConnection: {}\r\n\r\n",
        body.len(),
        if keep_alive { "keep-alive" } else { "close" }
    );
    socket.write_all(header.as_bytes()).await.unwrap();
    socket.write_all(body).await.unwrap();
}
async fn respond(socket: &mut TcpStream, status: &str, content_type: &str, body: &str) {
    respond_bytes(
        socket,
        status,
        &format!("Content-Type: {content_type}\r\n"),
        body.as_bytes(),
        false,
    )
    .await;
}
fn done() -> String {
    format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({"id":"request","choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]})
    )
}
async fn events(mut stream: ProviderStream) -> Vec<ModelStreamEvent> {
    let mut out = vec![];
    while let Some(event) = stream.recv().await {
        out.push(event.event().clone());
    }
    stream.join().await;
    out
}

#[tokio::test]
async fn pooled_http_preserves_endpoint_credentials_and_disables_redirect_retries() {
    tokio::time::timeout(Duration::from_secs(5), async {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let task = tokio::spawn(async move {
            // Only one accepted socket: profiles with different credentials share a client pool.
            let (mut socket, _) = listener.accept().await.unwrap();
            for (i, credential) in [
                "first-fixture-secret",
                "second-fixture-secret",
                "first-fixture-secret",
            ]
            .iter()
            .enumerate()
            {
                let raw = read_request(&mut socket).await;
                let text = String::from_utf8(raw).unwrap();
                assert!(text.starts_with("POST /v1/chat/completions HTTP/1.1"));
                let authorization: Vec<_> = text
                    .lines()
                    .filter(|line| line.to_ascii_lowercase().starts_with("authorization:"))
                    .collect();
                assert_eq!(
                    authorization,
                    vec![format!("authorization: Bearer {credential}")]
                );
                if i < 2 {
                    respond_bytes(
                        &mut socket,
                        "200 OK",
                        "Content-Type: application/json\r\n",
                        br#"{"choices":[{"message":{"content":"Done"},"finish_reason":"stop"}]}"#,
                        true,
                    )
                    .await;
                } else {
                    respond_bytes(
                        &mut socket,
                        "307 Temporary Redirect",
                        "Location: /should-not-follow\r\n",
                        b"{}",
                        false,
                    )
                    .await;
                }
            }
            assert!(
                tokio::time::timeout(Duration::from_millis(100), listener.accept())
                    .await
                    .is_err()
            );
        });
        let pool = pool(PoolLimits::default());
        let first = credential_provider(
            format!("http://{address}/v1"),
            pool.clone(),
            3,
            "first-fixture-secret",
        );
        let second = credential_provider(
            format!("http://{address}/v1"),
            pool.clone(),
            3,
            "second-fixture-secret",
        );
        assert!(!format!("{first:?}{:?}", second.config()).contains("fixture-secret"));
        assert_eq!(
            first
                .complete(&request(), &Cancellation::new())
                .await
                .unwrap()
                .text,
            "Done"
        );
        assert_eq!(
            second
                .complete(&request(), &Cancellation::new())
                .await
                .unwrap()
                .text,
            "Done"
        );
        // HTTPX does not follow redirects, and 3xx JSON is still parsed by this source adapter.
        assert_eq!(
            first
                .complete(&request(), &Cancellation::new())
                .await
                .unwrap()
                .text,
            ""
        );
        task.await.unwrap();
        assert_eq!(pool.active_connections(), 0);
        assert_eq!(pool.active_requests(), 0);
    })
    .await
    .unwrap();
}
#[tokio::test]
async fn provider_retries_only_before_output_and_keeps_parent_cancellation_independent() {
    tokio::time::timeout(Duration::from_secs(8), async {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let seen = count.clone();
        let task = tokio::spawn(async move {
            for i in 0..7 {
                let (mut socket, _) = listener.accept().await.unwrap();
                read_request(&mut socket).await;
                seen.fetch_add(1, Ordering::SeqCst);
                let error = json!({"error":{"code":503,"message":"busy"}});
                let body = match i {
                    0 => format!(
                        "data: {}\n\n",
                        json!({"choices":[{"delta":{},"finish_reason":"network_error"}]})
                    ),
                    1 => done(),
                    2 => format!(
                        "data: {}\n\ndata: {error}\n\n",
                        json!({"choices":[{"delta":{"content":"Partial"}}]})
                    ),
                    3 => format!(
                        "data: {}\n\n",
                        json!({"choices":[{"delta":{"content":"Incomplete"}}]})
                    ),
                    4 => format!("data: {}\n\ndata: {error}\n\n", json!({"choices":[null]})),
                    5 => format!(
                        "data: {}\n\ndata: {error}\n\n",
                        json!({"choices":{"malformed":true}})
                    ),
                    _ => format!("{}data: {error}\n\n", "data:  \n\n".repeat(33)),
                };
                respond(&mut socket, "200 OK", "text/event-stream", &body).await;
            }
            assert!(
                tokio::time::timeout(Duration::from_millis(100), listener.accept())
                    .await
                    .is_err()
            );
        });
        let pool = pool(PoolLimits::default());
        let p = provider(format!("http://{address}/beta"), pool.clone(), 3);
        let parent = Cancellation::new();
        let first = events(p.stream(request(), parent.clone()).unwrap()).await;
        assert_eq!(
            first
                .iter()
                .filter(|e| e.event_type == EventType::Started)
                .count(),
            1
        );
        assert_eq!(first.last().unwrap().event_type, EventType::Completed);
        assert!(!parent.is_cancelled());
        let second = events(p.stream(request(), parent.clone()).unwrap()).await;
        assert!(second.iter().any(|e| e.delta.as_deref() == Some("Partial")));
        assert_eq!(
            second.last().unwrap().error_kind,
            Some(ErrorKind::Overloaded)
        );
        let incomplete = events(p.stream(request(), parent.clone()).unwrap()).await;
        assert!(
            incomplete
                .iter()
                .any(|e| e.delta.as_deref() == Some("Incomplete"))
        );
        assert_eq!(incomplete.last().unwrap().event_type, EventType::Error);
        assert_eq!(incomplete.last().unwrap().error_kind, None);
        assert!(
            !incomplete
                .iter()
                .any(|e| e.event_type == EventType::Completed)
        );
        // Malformed choices end the source retry window; 32 leading whitespace frames
        // also hand over under the explicit bounded extension instead of accumulating forever.
        for _ in 0..3 {
            let result = events(p.stream(request(), parent.clone()).unwrap()).await;
            assert_eq!(
                result.last().unwrap().error_kind,
                Some(ErrorKind::Overloaded)
            );
            assert_eq!(
                result
                    .iter()
                    .filter(|e| e.event_type == EventType::Started)
                    .count(),
                1
            );
            assert!(!parent.is_cancelled());
        }
        task.await.unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 7);
        assert_eq!(pool.active_requests(), 0);
        assert_eq!(pool.active_connections(), 0);
    })
    .await
    .unwrap();
}
#[tokio::test]
async fn bounded_stream_cancellation_releases_admission_and_connection_without_leaking_payload() {
    tokio::time::timeout(Duration::from_secs(8),async {
        let listener=TcpListener::bind("127.0.0.1:0").await.unwrap();let address=listener.local_addr().unwrap();let(ready_tx,mut ready_rx)=mpsc::channel(1);
        let task=tokio::spawn(async move {
            let(mut socket,_)=listener.accept().await.unwrap();read_request(&mut socket).await;
            socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n").await.unwrap();
            let payload=format!("data: {}\n\n",json!({"choices":[{"delta":{"content":"do-not-print"}}]}));
            for _ in 0..64 {let chunk=format!("{:x}\r\n{payload}\r\n",payload.len());if socket.write_all(chunk.as_bytes()).await.is_err(){break}}
            ready_tx.send(()).await.unwrap();let mut byte=[0u8;1];let _=tokio::time::timeout(Duration::from_secs(2),socket.read(&mut byte)).await;
        });
        let limits=PoolLimits{connections:1,admitted_requests:1,channel_events:1,channel_bytes:1024,..PoolLimits::default()};let shared=pool(limits);
        let p=provider(format!("http://{address}"),shared.clone(),1);let parent=Cancellation::new();let mut stream=p.stream(request(),parent.clone()).unwrap();
        let started=stream.recv().await.unwrap();assert_eq!(started.event().event_type,EventType::Started);drop(started);ready_rx.recv().await.unwrap();
        assert_eq!(p.stream(request(),Cancellation::new()).err().unwrap().kind,ErrorKind::Capacity);
        let event=stream.recv().await.unwrap();assert!(!format!("{event:?}").contains("do-not-print"));drop(event);drop(stream);assert!(!parent.is_cancelled());
        tokio::time::timeout(Duration::from_secs(2),async{while shared.active_requests()!=0{tokio::task::yield_now().await;}}).await.unwrap();assert_eq!(shared.active_connections(),0);task.await.unwrap();
        let p=provider("http://127.0.0.1:9".into(),pool(PoolLimits{memory_bytes:1024,..PoolLimits::default()}),1);
        let large=ModelRequest::text("fixture",vec![ModelMessage{role:"user".into(),content:json!("x".repeat(2048))}]);
        assert_eq!(p.stream(large,Cancellation::new()).err().unwrap().kind,ErrorKind::Capacity);
        let listener=TcpListener::bind("127.0.0.1:0").await.unwrap();let address=listener.local_addr().unwrap();
        let task=tokio::spawn(async move {
            // Reproducible gzip of 4096 'x' bytes: wire bytes fit, decoded body exceeds 64.
            let compressed=[31,139,8,0,0,0,0,0,2,3,237,193,1,13,0,0,0,194,160,218,143,111,15,7,20,0,0,0,240,110,193,119,16,62,0,16,0,0];
            for body in [&compressed[..],b"not-a-gzip-body".as_slice()] {
                let(mut socket,_)=listener.accept().await.unwrap();read_request(&mut socket).await;
                respond_bytes(&mut socket,"200 OK","Content-Type: application/json\r\nContent-Encoding: gzip\r\n",body,false).await;
            }
            assert!(tokio::time::timeout(Duration::from_millis(100),listener.accept()).await.is_err());
        });
        let shared=pool(PoolLimits{protocol:ProtocolLimits{response_bytes:64,..ProtocolLimits::default()},..PoolLimits::default()});
        let p=provider(format!("http://{address}"),shared.clone(),3);
        assert_eq!(p.complete(&request(),&Cancellation::new()).await.unwrap_err().kind,ErrorKind::Response);
        assert_eq!(p.complete(&request(),&Cancellation::new()).await.unwrap_err().kind,ErrorKind::Response);
        task.await.unwrap();assert_eq!(shared.active_requests(),0);assert_eq!(shared.active_connections(),0);
    }).await.unwrap();
}

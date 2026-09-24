use crate::{
    contracts::{bounded_json, json_len},
    openai::{
        delta_output, error_frame, finish_failure, frame_data, frame_output, http_error_kind,
    },
    *,
};
use futures_util::StreamExt;
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, watch};

#[derive(Clone, Debug)]
pub struct Cancellation {
    sender: watch::Sender<bool>,
}
impl Default for Cancellation {
    fn default() -> Self {
        Self::new()
    }
}
impl Cancellation {
    pub fn new() -> Self {
        let (sender, _) = watch::channel(false);
        Self { sender }
    }
    pub fn cancel(&self) {
        self.sender.send_replace(true);
    }
    pub fn is_cancelled(&self) -> bool {
        *self.sender.borrow()
    }
    pub async fn cancelled(&self) {
        let mut r = self.sender.subscribe();
        while !*r.borrow_and_update() {
            if r.changed().await.is_err() {
                return;
            }
        }
    }
}
struct CancelPair {
    parent: Cancellation,
    internal: Cancellation,
}
impl CancelPair {
    async fn cancelled(&self) {
        tokio::select! {_=self.parent.cancelled()=>{},_=self.internal.cancelled()=>{}}
    }
}
#[derive(Clone, Debug)]
pub struct PoolLimits {
    pub connections: usize,
    pub admitted_requests: usize,
    pub request_bytes: usize,
    pub memory_bytes: usize,
    pub channel_bytes: usize,
    pub channel_events: usize,
    pub protocol: ProtocolLimits,
    pub trust_env: bool,
}
impl Default for PoolLimits {
    fn default() -> Self {
        Self {
            connections: 128,
            admitted_requests: 256,
            request_bytes: 4 * 1024 * 1024,
            memory_bytes: 256 * 1024 * 1024,
            channel_bytes: 64 * 1024 * 1024,
            channel_events: 32,
            protocol: ProtocolLimits::default(),
            trust_env: true,
        }
    }
}
/// Clients contain no per-profile secrets or cookie jar. Header snapshots are per request.
pub struct ProviderPool {
    limits: PoolLimits,
    clients: Mutex<HashMap<(String, Duration), reqwest::Client>>,
    connections: Arc<Semaphore>,
    admission: Arc<Semaphore>,
    memory: Arc<Semaphore>,
    channel: Arc<Semaphore>,
}
impl std::fmt::Debug for ProviderPool {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ProviderPool")
            .field("limits", &self.limits)
            .finish_non_exhaustive()
    }
}
impl ProviderPool {
    pub fn new(limits: PoolLimits) -> Result<Self> {
        if !(1..=2048).contains(&limits.connections)
            || !(1..=4096).contains(&limits.admitted_requests)
            || limits.connections > limits.admitted_requests
            || !(1..=1024).contains(&limits.channel_events)
            || !(1..=16 * 1024 * 1024).contains(&limits.request_bytes)
            || !(1024..=1024 * 1024 * 1024).contains(&limits.memory_bytes)
            || !(1024..=1024 * 1024 * 1024).contains(&limits.channel_bytes)
            || !(1..=16 * 1024 * 1024).contains(&limits.protocol.frame_bytes)
            || !(1..=16 * 1024 * 1024).contains(&limits.protocol.response_bytes)
            || limits.protocol.tool_calls > 10000
        {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "Invalid provider pool limits",
            ));
        }
        Ok(Self {
            connections: Arc::new(Semaphore::new(limits.connections)),
            admission: Arc::new(Semaphore::new(limits.admitted_requests)),
            memory: Arc::new(Semaphore::new(limits.memory_bytes)),
            channel: Arc::new(Semaphore::new(limits.channel_bytes)),
            clients: Mutex::new(HashMap::new()),
            limits,
        })
    }
    fn client(&self, url: &reqwest::Url, connect_timeout: Duration) -> Result<reqwest::Client> {
        let key = (url.origin().ascii_serialization(), connect_timeout);
        let mut clients = self.clients.lock().map_err(|_| ProviderError::capacity())?;
        if let Some(c) = clients.get(&key) {
            return Ok(c.clone());
        }
        if clients.len() >= 32 {
            return Err(ProviderError::capacity());
        }
        let mut builder = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .http1_only()
            .referer(false)
            .connect_timeout(connect_timeout)
            .pool_max_idle_per_host(20)
            .pool_idle_timeout(Duration::from_secs(5));
        if !self.limits.trust_env {
            builder = builder.no_proxy();
        }
        let client = builder.build().map_err(|_| {
            ProviderError::new(
                ErrorKind::Configuration,
                "Could not initialize provider HTTP transport",
            )
        })?;
        clients.insert(key, client.clone());
        Ok(client)
    }
    pub fn active_requests(&self) -> usize {
        self.limits.admitted_requests - self.admission.available_permits()
    }
    pub fn active_connections(&self) -> usize {
        self.limits.connections - self.connections.available_permits()
    }
}
struct Account {
    pool: Arc<Semaphore>,
    permit: Option<OwnedSemaphorePermit>,
    bytes: usize,
}
impl Account {
    fn new(pool: Arc<Semaphore>) -> Self {
        Self {
            pool,
            permit: None,
            bytes: 0,
        }
    }
    fn ensure(&mut self, bytes: usize) -> Result<()> {
        if bytes <= self.bytes {
            return Ok(());
        }
        let delta = u32::try_from(bytes - self.bytes).map_err(|_| ProviderError::capacity())?;
        let p = self
            .pool
            .clone()
            .try_acquire_many_owned(delta)
            .map_err(|_| ProviderError::capacity())?;
        if let Some(permit) = &mut self.permit {
            permit.merge(p)
        } else {
            self.permit = Some(p)
        }
        self.bytes = bytes;
        Ok(())
    }
}
#[derive(Clone)]
pub struct OpenAiProvider {
    config: ProviderConfig,
    pool: Arc<ProviderPool>,
    url: reqwest::Url,
}
impl std::fmt::Debug for OpenAiProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpenAiProvider").finish_non_exhaustive()
    }
}
impl OpenAiProvider {
    pub fn new(config: ProviderConfig, pool: Arc<ProviderPool>) -> Result<Self> {
        let mut url = reqwest::Url::parse(&config.base_url).map_err(|_| {
            ProviderError::new(ErrorKind::Configuration, "Invalid provider endpoint")
        })?;
        if !["http", "https"].contains(&url.scheme())
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || config.timeout.is_zero()
            || config.timeout > Duration::from_secs(900)
            || config.completion_timeout.is_zero()
            || config.completion_timeout > Duration::from_secs(3600)
            || !(1..=8).contains(&config.retry_attempts)
            || config.retry_backoff > Duration::from_secs(20)
        {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "Invalid provider transport configuration",
            ));
        }
        let path = url.path().trim_end_matches('/');
        let last = path.rsplit('/').next().unwrap_or("");
        let version = last == "beta"
            || last.strip_prefix('v').is_some_and(|s| {
                s.as_bytes().first().is_some_and(u8::is_ascii_digit)
                    && s.bytes().all(|b| b.is_ascii_alphanumeric())
            });
        let suffix = if version {
            "/chat/completions"
        } else {
            "/v1/chat/completions"
        };
        url.set_path(&format!("{path}{suffix}"));
        Ok(Self { config, pool, url })
    }
    pub fn config(&self) -> &ProviderConfig {
        &self.config
    }
    pub fn stream(&self, request: ModelRequest, cancel: Cancellation) -> Result<ProviderStream> {
        let admission = self
            .pool
            .admission
            .clone()
            .try_acquire_owned()
            .map_err(|_| ProviderError::capacity())?;
        let mut account = Account::new(self.pool.memory.clone());
        let size = json_len(&request, self.pool.limits.request_bytes)?;
        account.ensure(size.saturating_mul(4))?;
        let payload = openai_payload(&self.config, &request, true)?;
        let body = bounded_json(&payload, self.pool.limits.request_bytes)?;
        account.ensure(body.len().saturating_mul(4))?;
        drop(payload);
        let (tx, rx) = mpsc::channel(self.pool.limits.channel_events);
        let internal = Cancellation::new();
        let pair = CancelPair {
            parent: cancel,
            internal: internal.clone(),
        };
        let provider = self.clone();
        let task = tokio::spawn(async move {
            let _admission = admission;
            let result = provider
                .run_stream(request, body, &pair, &tx, &mut account)
                .await;
            if let Err(error) = result
                && error.kind != ErrorKind::Cancelled
            {
                let _ = provider
                    .send(&tx, ModelStreamEvent::failure(error), &pair)
                    .await;
            }
        });
        Ok(ProviderStream {
            rx,
            internal,
            task: Some(task),
        })
    }
    /// The caller must charge retained result ownership in its context budget.
    pub async fn complete(
        &self,
        request: &ModelRequest,
        cancel: &Cancellation,
    ) -> Result<ModelResponse> {
        let _admission = self
            .pool
            .admission
            .clone()
            .try_acquire_owned()
            .map_err(|_| ProviderError::capacity())?;
        let pair = CancelPair {
            parent: cancel.clone(),
            internal: Cancellation::new(),
        };
        let mut account = Account::new(self.pool.memory.clone());
        let size = json_len(request, self.pool.limits.request_bytes)?;
        account.ensure(size.saturating_mul(4))?;
        let body = bounded_json(
            &openai_payload(&self.config, request, false)?,
            self.pool.limits.request_bytes,
        )?;
        let base = body.len().saturating_mul(4);
        account.ensure(base)?;
        let mut attempt = 1;
        loop {
            let result = self
                .complete_attempt(request, &body, &pair, &mut account, base)
                .await;
            match result {
                Ok(response) => return Ok(response),
                Err(e) if can_retry(&e) && attempt < self.config.retry_attempts => {
                    self.backoff(attempt, e.retry_after, &pair).await?;
                    attempt += 1
                }
                Err(e) => return Err(e),
            }
        }
    }
    async fn connection(&self, cancel: &CancelPair) -> Result<OwnedSemaphorePermit> {
        tokio::select! {biased;_=cancel.cancelled()=>Err(ProviderError::cancelled()),p=self.pool.connections.clone().acquire_owned()=>p.map_err(|_|ProviderError::capacity())}
    }
    async fn response(
        &self,
        body: &[u8],
        stream: bool,
        cancel: &CancelPair,
    ) -> Result<reqwest::Response> {
        let idle = if stream {
            self.config.timeout
        } else {
            self.config.completion_timeout
        };
        let client = self.pool.client(
            &self.url,
            if stream {
                self.config.timeout
            } else {
                Duration::from_secs(10)
            },
        )?;
        let request = client
            .post(self.url.clone())
            .headers(self.config.headers.clone())
            .header("content-type", "application/json")
            .body(body.to_vec())
            .send();
        tokio::select! {biased;_=cancel.cancelled()=>Err(ProviderError::cancelled()),r=tokio::time::timeout(idle,request)=>match r{Ok(Ok(r))=>Ok(r),Ok(Err(e))=>Err(transport_error(e)),Err(_)=>Err(ProviderError::new(ErrorKind::Transport,"Provider response headers timed out"))}}
    }
    async fn body(
        &self,
        response: reqwest::Response,
        timeout: Duration,
        cancel: &CancelPair,
        account: &mut Account,
        base: usize,
    ) -> Result<Vec<u8>> {
        let mut stream = response.bytes_stream();
        let mut body = vec![];
        loop {
            let next = tokio::select! {biased;_=cancel.cancelled()=>return Err(ProviderError::cancelled()),r=tokio::time::timeout(timeout,stream.next())=>r.map_err(|_|ProviderError::new(ErrorKind::Transport,"Provider response timed out"))?};
            let Some(chunk) = next else { break };
            let chunk = chunk.map_err(transport_error)?;
            if chunk.len()
                > self
                    .pool
                    .limits
                    .protocol
                    .response_bytes
                    .saturating_sub(body.len())
            {
                return Err(ProviderError::response(
                    "Provider response exceeded its byte limit",
                ));
            }
            account.ensure(base + (body.len() + chunk.len()).saturating_mul(64))?;
            body.extend_from_slice(&chunk);
        }
        Ok(body)
    }
    async fn complete_attempt(
        &self,
        request: &ModelRequest,
        body: &[u8],
        cancel: &CancelPair,
        account: &mut Account,
        base: usize,
    ) -> Result<ModelResponse> {
        let _connection = self.connection(cancel).await?;
        let response = self.response(body, false, cancel).await?;
        let status = response.status().as_u16();
        let retry = retry_after(response.headers());
        let bytes = self
            .body(
                response,
                self.config.completion_timeout,
                cancel,
                account,
                base,
            )
            .await?;
        if status >= 400 {
            return Err(http_error(status, &bytes, retry));
        }
        decode_completion(&self.config, request, &bytes, self.pool.limits.protocol)
    }
    async fn send(
        &self,
        tx: &mpsc::Sender<ReceivedEvent>,
        event: ModelStreamEvent,
        cancel: &CancelPair,
    ) -> Result<()> {
        let size = event_memory(
            &event,
            self.pool.limits.protocol.response_bytes.saturating_mul(2),
        )?;
        if size > self.pool.limits.channel_bytes {
            return Err(ProviderError::capacity());
        }
        let permit = tokio::select! {biased;_=cancel.cancelled()=>return Err(ProviderError::cancelled()),p=self.pool.channel.clone().acquire_many_owned(size as u32)=>p.map_err(|_|ProviderError::capacity())?};
        tokio::select! {biased;_=cancel.cancelled()=>Err(ProviderError::cancelled()),r=tx.send(ReceivedEvent{event,_permit:permit})=>r.map_err(|_|ProviderError::cancelled())}
    }
    async fn run_stream(
        &self,
        request: ModelRequest,
        body: Vec<u8>,
        cancel: &CancelPair,
        tx: &mpsc::Sender<ReceivedEvent>,
        account: &mut Account,
    ) -> Result<()> {
        self.send(tx, ModelStreamEvent::started(), cancel).await?;
        let base = account.bytes;
        let mut attempt = 1;
        loop {
            let mut handed_over = false;
            let result = self
                .stream_attempt(
                    (&request, &body),
                    cancel,
                    tx,
                    account,
                    base,
                    &mut handed_over,
                )
                .await;
            match result {
                Ok(()) => return Ok(()),
                Err(e) if !handed_over && can_retry(&e) && attempt < self.config.retry_attempts => {
                    self.backoff(attempt, e.retry_after, cancel).await?;
                    attempt += 1
                }
                Err(e) => return Err(e),
            }
        }
    }
    async fn stream_attempt(
        &self,
        input: (&ModelRequest, &[u8]),
        cancel: &CancelPair,
        tx: &mpsc::Sender<ReceivedEvent>,
        account: &mut Account,
        base: usize,
        handed: &mut bool,
    ) -> Result<()> {
        let (request, body) = input;
        let _connection = self.connection(cancel).await?;
        let response = self.response(body, true, cancel).await?;
        let status = response.status().as_u16();
        if status >= 400 {
            let retry = retry_after(response.headers());
            let bytes = self
                .body(response, self.config.timeout, cancel, account, base)
                .await?;
            return Err(http_error(status, &bytes, retry));
        }
        let mut input = response.bytes_stream();
        let limits = self.pool.limits.protocol;
        let mut decoder = SseDecoder::new(limits.frame_bytes, limits.events_per_chunk)?;
        let mut accumulator = OpenAiAccumulator::new(self.config.clone(), request.clone(), limits)?;
        let mut leading: Vec<String> = vec![];
        let mut leading_bytes = 0usize;
        let mut eof = false;
        loop {
            let next = tokio::select! {biased;_=cancel.cancelled()=>return Err(ProviderError::cancelled()),r=tokio::time::timeout(self.config.timeout,input.next())=>r};
            let frames = match next {
                Ok(Some(Ok(chunk))) => {
                    if chunk.len() > limits.response_bytes {
                        return Err(ProviderError::capacity());
                    }
                    account.ensure(
                        base + accumulator.retained_bytes().saturating_mul(4)
                            + leading_bytes.saturating_mul(2)
                            + chunk.len().saturating_mul(64)
                            + decoder.retained_bytes().saturating_mul(4),
                    )?;
                    decoder.push_reply(&chunk)?
                }
                Ok(Some(Err(error))) => {
                    if accumulator.has_finish_reason() {
                        break;
                    }
                    return Err(transport_error(error));
                }
                Err(_) => {
                    if accumulator.has_finish_reason() {
                        break;
                    }
                    return Err(ProviderError::new(
                        ErrorKind::Transport,
                        "Provider stream timed out",
                    ));
                }
                Ok(None) => {
                    eof = true;
                    decoder.finish()?
                }
            };
            for frame in frames {
                let mut frames = vec![];
                if !*handed {
                    leading_bytes = leading_bytes.saturating_add(frame.len());
                    if leading.len() >= 32 || leading_bytes > limits.frame_bytes {
                        return Err(ProviderError::response(
                            "Provider leading frames exceeded their bound",
                        ));
                    }
                    let stripped = frame.trim();
                    let data = if stripped == "[DONE]" {
                        None
                    } else {
                        frame_data(stripped).ok().flatten()
                    };
                    if let Some(error) = data.as_ref().and_then(|v| {
                        error_frame(v).or_else(|| {
                            let choice = v["choices"]
                                .as_array()
                                .and_then(|a| a.first())
                                .unwrap_or(&serde_json::Value::Null);
                            if !delta_output(&choice["delta"]) {
                                finish_failure(choice)
                            } else {
                                None
                            }
                        })
                    }) && error.kind == ErrorKind::Overloaded
                    {
                        return Err(error);
                    }
                    let output = data.as_ref().is_some_and(frame_output);
                    let bad = frame_data(stripped).is_err();
                    let terminal = stripped == "[DONE]";
                    let semantic_error = data.as_ref().and_then(error_frame).is_some();
                    leading.push(frame);
                    if output || bad || terminal || semantic_error || leading.len() >= 32 {
                        *handed = true;
                        frames = std::mem::take(&mut leading);
                        leading_bytes = 0;
                    }
                } else {
                    frames.push(frame)
                }
                for frame in frames {
                    let events = accumulator.push(&frame)?;
                    account.ensure(
                        base + accumulator.retained_bytes().saturating_mul(4)
                            + decoder.retained_bytes().saturating_mul(4),
                    )?;
                    for event in events {
                        self.send(tx, event, cancel).await?;
                    }
                    if accumulator.terminated() {
                        break;
                    }
                }
                if accumulator.terminated() {
                    break;
                }
            }
            if accumulator.terminated() || eof {
                break;
            }
        }
        if !*handed {
            *handed = true;
            for frame in leading {
                for event in accumulator.push(&frame)? {
                    self.send(tx, event, cancel).await?;
                }
            }
        }
        for event in accumulator.finish()? {
            self.send(tx, event, cancel).await?;
        }
        Ok(())
    }
    async fn backoff(&self, attempt: u8, retry: Option<f64>, cancel: &CancelPair) -> Result<()> {
        let base = (self.config.retry_backoff.as_secs_f64() * 2f64.powi(attempt as i32 - 1))
            .min(20.0)
            .max(retry.unwrap_or(0.0).min(20.0));
        let random = uuid::Uuid::new_v4().as_u128() as u64;
        let jitter = (random as f64 / u64::MAX as f64) * 0.25 * base;
        tokio::select! {biased;_=cancel.cancelled()=>Err(ProviderError::cancelled()),_=tokio::time::sleep(Duration::from_secs_f64(base+jitter))=>Ok(())}
    }
}
fn event_memory(event: &ModelStreamEvent, limit: usize) -> Result<usize> {
    let mut size = json_len(event, limit)?.saturating_mul(4).max(1);
    let mut metadata = |value: Option<&serde_json::Value>| -> Result<()> {
        if let Some(value) = value {
            size = size.saturating_add(json_len(value, 256 * 1024)?.saturating_mul(16));
        }
        Ok(())
    };
    if let Some(call) = &event.tool_call {
        metadata(call.provider_metadata.as_ref())?;
    }
    if let Some(response) = &event.response {
        metadata(response.reasoning_state.as_ref())?;
        for call in &response.tool_calls {
            metadata(call.provider_metadata.as_ref())?;
        }
    }
    Ok(size)
}

fn can_retry(error: &ProviderError) -> bool {
    matches!(error.kind, ErrorKind::Overloaded | ErrorKind::Transport)
}
fn transport_error(error: reqwest::Error) -> ProviderError {
    ProviderError::new(
        if error.is_decode() {
            ErrorKind::Response
        } else if error.is_builder() {
            ErrorKind::Configuration
        } else {
            ErrorKind::Transport
        },
        if error.is_timeout() {
            "Provider connection timed out"
        } else {
            "Provider connection failed"
        },
    )
}
fn retry_after(headers: &reqwest::header::HeaderMap) -> Option<f64> {
    let raw = headers.get("retry-after")?.to_str().ok()?;
    if let Ok(n) = raw.parse::<f64>() {
        return n.is_finite().then(|| n.clamp(0.0, 20.0));
    }
    let date = chrono::DateTime::parse_from_rfc2822(raw).ok()?;
    Some(
        (date
            .signed_duration_since(chrono::Utc::now())
            .num_milliseconds() as f64
            / 1000.0)
            .clamp(0.0, 20.0),
    )
}
fn http_error(status: u16, bytes: &[u8], retry: Option<f64>) -> ProviderError {
    let parsed = serde_json::from_slice::<serde_json::Value>(bytes).ok();
    let kind = http_error_kind(status, parsed.as_ref().unwrap_or(&serde_json::Value::Null));
    ProviderError {
        kind,
        detail: format!("provider returned HTTP {status}"),
        status_code: Some(status),
        retry_after: retry,
    }
}
/// Runtime retains this byte lease until it has durably consumed the event.
pub struct ReceivedEvent {
    event: ModelStreamEvent,
    _permit: OwnedSemaphorePermit,
}
impl ReceivedEvent {
    pub fn event(&self) -> &ModelStreamEvent {
        &self.event
    }
}
impl std::fmt::Debug for ReceivedEvent {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReceivedEvent")
            .field("type", &self.event.event_type)
            .finish_non_exhaustive()
    }
}
pub struct ProviderStream {
    rx: mpsc::Receiver<ReceivedEvent>,
    internal: Cancellation,
    task: Option<tokio::task::JoinHandle<()>>,
}
impl ProviderStream {
    pub async fn recv(&mut self) -> Option<ReceivedEvent> {
        self.rx.recv().await
    }
    pub fn cancel(&self) {
        self.internal.cancel()
    }
    pub async fn join(mut self) {
        self.rx.close();
        self.internal.cancel();
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}
impl Drop for ProviderStream {
    fn drop(&mut self) {
        self.internal.cancel();
    }
}

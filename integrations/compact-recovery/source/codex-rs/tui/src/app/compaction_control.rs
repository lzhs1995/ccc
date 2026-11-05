//! Native control requests use the original TUI selection; no slash insertion,
//! draft replacement, foreground observer replay, or synthetic user message.
use super::App;
use crate::app_server_session::AppServerSession;
use ccc_native_client_foreground::compaction_socket::NativeControlRequest;
use codex_app_server_protocol::ThreadCompactRecoveryParams;
use serde_json::Value;
use serde_json::json;
use std::time::Duration;

impl App {
    pub(super) async fn handle_native_compaction_control(
        &mut self,
        backend: &mut AppServerSession,
        request: NativeControlRequest,
    ) {
        let result = self
            .native_compaction_control_inner(backend, &request)
            .await;
        let response = match result {
            Ok(value) => value,
            Err(error) => json!({"error":error,"replayAllowed":false}),
        };
        let _ = request.reply.send(response);
    }

    async fn native_compaction_control_inner(
        &mut self,
        backend: &mut AppServerSession,
        incoming: &NativeControlRequest,
    ) -> Result<Value, String> {
        if !incoming.queue_clear
            || !self.native_compaction_backend_matches(backend.backend_id())
            || self.reconnect.offline
            || self.reconnect.failed
            || self.native_foreground_has_barrier(true, true)
            || !self.chat_widget.native_compaction_input_clear()
            || self
                .active_thread_rx
                .as_ref()
                .is_none_or(|rx| !rx.is_empty())
        {
            return Err(
                "native control refused: draft, dialog, pending input or transition".into(),
            );
        }
        let thread = self.active_thread_id.ok_or("no selected native thread")?;
        if self.chat_widget.thread_id() != Some(thread)
            || !self.native_foreground_selection_allows_direct_input(thread)
            || self.thread_unavailable(thread)
            || self.agent_navigation.is_parent_owned(thread)
        {
            return Err("native selected thread is not an original direct-input owner".into());
        }
        let selection = self
            .native_compaction_observer()
            .compaction_selection()
            .map_err(|e| e.to_string())?;
        if incoming.body["clientEpoch"] != selection["expectedClientEpoch"]
            || incoming.body["threadId"] != json!(thread.to_string())
        {
            return Err("native control socket epoch/selected thread changed".into());
        }
        let method = incoming.body["method"]
            .as_str()
            .ok_or("native control method missing")?;
        if method == "compaction/recovery/query" {
            let params = ThreadCompactRecoveryParams {
                thread_id: thread.to_string(),
                expected_native_epoch: None,
                expected_turn_id: None,
                expected_occurrence: None,
                expected_generation: None,
                attempt_id: None,
                authority: None,
            };
            let native = tokio::time::timeout(
                Duration::from_secs(3),
                backend.thread_compact_recovery(params),
            )
            .await
            .map_err(|_| "native recovery query unavailable")?
            .map_err(|e| e.to_string())?;
            return Ok(json!({"selection":selection,"native":native,"nativeControlSocket":true}));
        }
        if method != "compaction/recovery/control" {
            return Err("unsupported native control method".into());
        }
        let mut control = incoming.body["request"].clone();
        let expected = &control["expected"];
        if expected["threadId"] != json!(thread.to_string()) {
            return Err("original compact actor thread changed".into());
        }
        let mut params = ThreadCompactRecoveryParams {
            thread_id: thread.to_string(),
            expected_native_epoch: Some(
                expected["nativeEpoch"]
                    .as_str()
                    .ok_or("native actor epoch missing")?
                    .into(),
            ),
            expected_turn_id: Some(
                expected["turnId"]
                    .as_str()
                    .ok_or("original turn missing")?
                    .into(),
            ),
            expected_occurrence: Some(
                expected["occurrence"]
                    .as_str()
                    .ok_or("original occurrence missing")?
                    .into(),
            ),
            expected_generation: Some(
                expected["generation"]
                    .as_u64()
                    .ok_or("provider generation missing")?,
            ),
            attempt_id: Some(
                control["attemptId"]
                    .as_str()
                    .ok_or("original attempt missing")?
                    .into(),
            ),
            authority: None,
        };
        // This call checks the in-memory original committed selection and consumes
        // a unique grant path. Even a later refused/unknown RPC cannot replay it.
        let grant = self
            .native_compaction_observer()
            .authorize_compaction(
                &self.config.codex_home,
                &control,
                incoming.peer_pid,
                incoming.peer_uid,
            )
            .map_err(|e| e.to_string())?;
        control["authority"]["proofPath"] = json!(grant.proof_path);
        control["authority"]["proofSha256"] = json!(grant.proof_sha256);
        params.authority = Some(control["authority"].clone());
        let observer = self.native_compaction_observer().clone();
        let call = backend.thread_compact_recovery(params);
        tokio::pin!(call);
        let deadline = tokio::time::sleep(Duration::from_secs(5));
        tokio::pin!(deadline);
        let mut heartbeat = tokio::time::interval(Duration::from_secs(1));
        let result = loop {
            tokio::select! {
                result=&mut call=>break Ok(result),
                _=&mut deadline=>break Err(()),
                _=heartbeat.tick()=>{ let _=observer.heartbeat(); }
            }
        };
        // The original actor retains its independently verified SH guard across
        // drain and replacement dispatch. Dropping this local guard is not a
        // completed-interruption receipt or permission for a terminal fallback.
        drop(grant.guard);
        match result {
            Ok(Ok(value)) => Ok(
                json!({"native":value,"proofPath":control["authority"]["proofPath"],
                "proofSha256":control["authority"]["proofSha256"],"replayAllowed":false}),
            ),
            Ok(Err(error)) => Err(format!(
                "native recovery refused/unknown; preserve original attempt: {error}"
            )),
            Err(_) => Err("native recovery result UNKNOWN; preserve original attempt".into()),
        }
    }
}

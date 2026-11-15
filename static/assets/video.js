/* ==========================================================================
   Rozaneh · روزنه — video analyzer panel (v1.04)
   Kept in its own file so app.js stays readable. Loaded after app.js; all
   helpers come from window.Rozaneh (html escaping, t(), api(), icons…).
   ========================================================================== */
(function () {
  "use strict";

const {
  $, h, t, num, ico, api, toast, errMsg, confirmModal, field, card, pageHead,
  toggleEl, emptyBox, fmtDate, fmtLogTime, ICO_CHEV, state, reload, fd,
} = window.Rozaneh;

const VIDEO_SRC_KEY = {
  youtube: "video.source_youtube", vimeo: "video.source_vimeo", direct: "video.source_direct",
  page: "video.source_page", upload: "video.source_upload", unknown: "video.source_unknown",
};

function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (!s) return "—";
  const H = Math.floor(s / 3600), M = Math.floor((s % 3600) / 60), S2 = s % 60;
  const pad = (n) => (n < 10 ? "0" + n : String(n));
  return H ? H + ":" + pad(M) + ":" + pad(S2) : M + ":" + pad(S2);
}
function fmtBytes(n) {
  const b = Number(n) || 0;
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return (i ? v.toFixed(1) : String(Math.round(v))) + " " + u[i];
}
function intervalOptions(current) {
  // keep a value that was set through the API selectable instead of silently
  // falling back to the first option
  const list = [5, 10, 15, 30, 60, 120, 240, 720];
  const n = Number(current);
  if (n && list.indexOf(n) === -1) list.push(n);
  return list.sort((a, b) => a - b);
}

function videoStatusChip(job) {
  const cls = job.status === "done" ? "ok" : job.status === "failed" ? "bad" : job.status === "cancelled" ? "" : "warn";
  return '<span class="chip ' + cls + '">' + h(t("video.status_" + job.status)) + "</span>";
}

function reportMetric(labelKey, value) {
  return '<div class="vmetric"><span>' + h(t(labelKey)) + "</span><b>" + h(value) + "</b></div>";
}

function reportBody(r) {
  r = r || {};
  const txt = r.text || {};
  const meta = [
    reportMetric("video.meta_duration", fmtDur(r.duration)),
    reportMetric("video.meta_size", fmtBytes(r.size)),
    reportMetric("video.meta_res", r.width ? r.width + "×" + r.height : "—"),
    reportMetric("video.meta_codec", [r.video_codec, r.audio_codec].filter(Boolean).join(" / ") || "—"),
    reportMetric("video.meta_words", num(txt.word_count || 0)),
    reportMetric("video.meta_lang", txt.language || "—"),
    reportMetric("video.meta_topic", txt.topic || "—"),
    reportMetric("video.meta_sentiment", txt.sentiment ? t("video.sent_" + txt.sentiment) : "—"),
  ].join("");

  const frames = (r.frames || []).map((f) =>
    '<figure class="vframe"><img loading="lazy" src="' + f.thumb + '" alt="">' +
    '<figcaption><span class="mono">' + fmtDur(f.at) + '</span>' +
    '<span class="vdot" style="background:' + h(f.dominant) + '"></span>' +
    '<span class="tiny mute">' + h(String(f.brightness)) + "</span></figcaption></figure>").join("");

  const keywords = (txt.keywords || []).map((k) => '<span class="kwchip">' + h(k) + "</span>").join("");
  const notes = (r.notes || []).map((n) => '<div class="list-line">' + ico("info") + "<span>" + h(n) + "</span></div>").join("");
  const transcript = (r.transcript || "").trim();

  return (
    '<div class="vreport">' +
      '<div class="vhero">' +
        (r.thumbnail ? '<img class="vthumb" loading="lazy" src="' + h(r.thumbnail) + '" alt="">' : '<div class="vthumb ph">' + ico("video") + "</div>") +
        "<div class=\"vhero-txt\">" +
          '<div class="inline tiny mute" style="gap:8px"><span class="chip">' + h(t(VIDEO_SRC_KEY[r.source] || "video.source_unknown")) + "</span>" +
          (r.author ? "<span>" + h(r.author) + "</span>" : "") +
          (r.summary_engine === "llm" || r.summary_engine === "video_ai" ? '<span class="chip ok">' + ico("sparkles") + " AI</span>" : '<span class="chip">' + ico("list") + " extractive</span>") + "</div>" +
          "<h3>" + h(r.title || "—") + "</h3>" +
          (r.description ? '<p class="vdesc">' + h(r.description.slice(0, 260)) + (r.description.length > 260 ? "…" : "") + "</p>" : "") +
        "</div>" +
      "</div>" +
      '<div class="vmetrics">' + meta + "</div>" +
      (r.ai_evidence ? '<div class="vevidence">' + ico("info") + '<span>' +
        h(t("video.evidence_" + r.ai_evidence.source)) + ' · ' + h(t("video.evidence_parts")) + ': ' + num(r.ai_evidence.segments) +
        ' · ' + h(t("video.evidence_frames")) + ': ' + num(r.ai_evidence.frames_sent) + '</span></div>' : '') +
      (r.summary ? '<section class="vblock"><h4>' + ico("sparkles") + h(t("video.summary")) + "</h4><p class=\"vsummary\">" + h(r.summary) + "</p>" +
        '<div class="inline"><button class="btn ghost sm" data-act="video.copy">' + ico("code") + "<span>" + h(t("video.copy_caption")) + "</span></button></div>" +
        '<pre class="vcap" id="videoCaption">' + h(r.caption || "") + "</pre></section>" : "") +
      (keywords ? '<section class="vblock"><h4>' + ico("tag") + h(t("video.keywords")) + "</h4><div class=\"kwrow\">" + keywords + "</div></section>" : "") +
      (frames ? '<section class="vblock"><h4>' + ico("video") + h(t("video.frames")) +
        (r.frames_meta && r.frames_meta.count ? '<span class="tiny mute">' + num(r.frames_meta.count) + " · ⌀" + h(String(r.frames_meta.avg_brightness || 0)) + "</span>" : "") +
        "</h4><div class=\"vgrid\">" + frames + "</div></section>" : "") +
      (transcript ? '<section class="vblock"><h4>' + ico("book") + h(t("video.transcript_title")) +
        '<span class="tiny mute">' + num((transcript || "").split(/\s+/).length) + " w</span></h4>" +
        '<details class="vdetails"><summary>' + h(t("video.transcript_title")) + "</summary><p class=\"vtrans\">" + h(transcript) + "</p></details></section>" : "") +
      (notes ? '<section class="vblock"><h4>' + ico("info") + "notes</h4>" + notes + "</section>" : "") +
    "</div>"
  );
}

function videoJobsHtml() {
  const jobs = (state.video.jobs || []).map((j) => {
    const rep = j.report || {};
    const open = state.video.activeJob === j.id;
    return (
      '<article class="vjob' + (open ? " open" : "") + '">' +
        '<div class="vjob-head" data-act="video.toggle" data-id="' + j.id + '">' +
          '<div class="vjob-thumb">' + (rep.thumbnail ? '<img loading="lazy" src="' + h(rep.thumbnail) + '" alt="">' : ico("video")) + "</div>" +
          '<div class="vjob-txt"><b>' + h(j.title || rep.title || j.url || "—") + "</b>" +
            '<div class="inline tiny mute" style="gap:6px">' + videoStatusChip(j) +
              '<span class="chip">' + h(t("video.mode_" + (j.mode === "auto" ? "auto" : j.mode === "api" ? "api" : "manual"))) + "</span>" +
              (rep.duration ? "<span>" + fmtDur(rep.duration) + "</span>" : "") +
              "<span>" + h(fmtDate(j.created_at)) + "</span>" +
            "</div>" +
          "</div>" +
          '<div class="vjob-acts">' +
            (j.status === "running" ? '<button class="btn danger xs icon" data-act="video.cancel" data-id="' + j.id + '" title="' + h(t("video.cancel")) + '">' + ico("x") + "</button>" : "") +
            '<button class="btn ghost xs icon" data-act="video.delete" data-id="' + j.id + '" title="' + h(t("video.delete")) + '">' + ico("trash") + "</button>" +
            '<span class="vchev">' + ICO_CHEV + "</span>" +
          "</div>" +
        "</div>" +
        (open ? '<div class="vjob-body">' + (j.status === "failed" ? '<div class="banner bad">' + ico("alert") + "<span>" + h(errMsg({ message: j.error || "unknown" })) + "</span></div>" : reportBody(rep)) + "</div>" : "") +
      "</article>"
    );
  }).join("");
  return jobs || emptyBox("video", t("video.empty"), t("video.no_timeout_note"));
}

function videoProgressHtml() {
  const running = (state.video.jobs || []).filter((j) => j.status === "running");
  const active = running[0] || null;
  const stages = ["queued", "fetching", "probing", "frames", "text", "summary", "done"];
  const stageIdx = active ? Math.max(0, stages.indexOf(active.stage)) : -1;
  if (!active) return "";
  return (
    '<div class="vprogress"><div class="vprog-head">' + ico("activity") + "<b>" + h(t("video.status_running")) + "</b>" +
      '<span class="chip">' + h(t("video.stage_" + (active.stage || "queued"))) + "</span>" +
      '<span class="tiny mute mono">#' + active.id + "</span>" +
      '<button class="btn danger xs" data-act="video.cancel" data-id="' + active.id + '">' + h(t("video.cancel")) + "</button></div>" +
      '<div class="vbar">' + stages.map((s2, i) => '<i class="' + (i <= stageIdx ? "on" : "") + '"></i>').join("") + "</div>" +
      '<div class="tiny mute">' + h(active.url || "") + "</div></div>"
  );
}

async function renderVideo(view) {
  if (!state.video.status) {
    view.innerHTML = '<div class="card"><div class="card-body"><div class="skeleton" style="width:40%"></div></div></div>';
  }
  await loadVideo();
  const anyRunning = (state.video.jobs || []).some((j) => j.status === "running");
  if (anyRunning) pollVideo();
  const st = state.video.status || {};
  const cfg = state.video.settings || {};
  const ff = st.ffmpeg;
  const smartCard =
    '<div class="vsmart">' +
      '<div class="vsmart-top">' +
        '<div class="vsmart-ico">' + ico("zap") + "</div>" +
        "<div><b>" + h(t("video.smart")) + "</b><p>" + h(t("video.smart_hint")) + "</p></div>" +
        toggleEl("video_smart", !!cfg.smart, "video.smart") +
      "</div>" +
      '<div class="vsmart-row">' +
        '<label class="tiny mute">' + h(t("video.interval")) + "</label>" +
        '<select data-act="video.interval">' +
          intervalOptions(cfg.interval_minutes).map((m) =>
            '<option value="' + m + '"' + (Number(cfg.interval_minutes) === m ? " selected" : "") + ">" + num(m) + "</option>").join("") +
        "</select>" +
        (cfg.last_smart_run ? '<span class="chip">' + ico("clock") + h(fmtLogTime(cfg.last_smart_run)) + "</span>" : "") +
      "</div>" +
      '<div class="vsmart-section"><div class="vsection-title"><div><b>' + h(t("video.ai_title")) + '</b><p>' + h(t("video.ai_hint")) + '</p></div>' +
        '<span class="chip ' + (cfg.ai_key_set ? 'ok' : 'warn') + '">' + h(t(cfg.ai_key_set ? 'video.key_set' : 'video.key_unset')) + '</span></div>' +
        '<form data-form="video-ai-settings" id="videoAiSettings">' +
          field(t("video.ai_url"), '<input class="ltr" type="url" name="base_url" required dir="ltr" value="' + h(cfg.ai_base_url || 'https://api.openai.com/v1') + '">') +
          '<div class="form-grid">' +
            field(t("video.ai_model"), '<input class="ltr" type="text" name="model" required dir="ltr" value="' + h(cfg.ai_model || 'gpt-4o-mini') + '">') +
            field(t("video.ai_key"), '<input class="ltr" type="password" name="api_key" dir="ltr" autocomplete="new-password" placeholder="' + h(t(cfg.ai_key_set ? 'video.key_keep' : 'video.key_enter')) + '">') +
          '</div>' +
          '<label class="vai-check"><input type="checkbox" name="vision"' + (cfg.ai_vision ? ' checked' : '') + '> <span>' + h(t("video.ai_vision")) + '</span></label>' +
          '<div class="vai-actions"><button type="submit" class="btn primary sm" data-act="submit" data-form-id="videoAiSettings">' + h(t("video.ai_save")) + '</button>' +
            '<button type="button" class="btn ghost sm" data-act="video.aiTest">' + h(t("video.ai_test")) + '</button>' +
            (cfg.ai_key_set ? '<button type="button" class="btn ghost sm" data-act="video.aiClear">' + h(t("video.ai_clear")) + '</button>' : '') + '</div>' +
        '</form></div>' +
      '<div class="vsmart-section"><div class="vsection-title"><div><b>' + h(t("video.quick_title")) + '</b><p>' + h(t("video.quick_hint")) + '</p></div></div>' +
        '<form data-form="video-quick" id="videoQuickForm" class="vquick">' +
          '<input class="ltr" type="url" name="url" required dir="ltr" placeholder="https://www.youtube.com/watch?v=..." aria-label="' + h(t("video.url")) + '">' +
          '<button type="submit" class="btn primary" data-act="submit" data-form-id="videoQuickForm">' + ico("play") + '<span>' + h(t("video.analyze")) + '</span></button>' +
        '</form><p class="vai-note">' + h(t("video.ai_limits")) + '</p></div>' +
    "</div>";

  const manualCard =
    '<form data-form="video" id="videoForm" class="vform">' +
      field(t("video.url"), '<input class="ltr" type="url" name="url" placeholder="' + h(t("video.url_ph")) + '" dir="ltr">') +
      '<div class="form-grid">' +
        field(t("video.title_hint"), '<input type="text" name="title" dir="auto">') +
        field(" ", '<button type="submit" class="btn primary lg block" data-act="submit" data-form-id="videoForm">' + ico("play") + "<span>" + h(t("video.analyze")) + "</span></button>") +
      "</div>" +
      field(t("video.transcript"), '<textarea name="transcript" rows="3" dir="auto" placeholder="' + h(t("video.transcript_ph")) + '"></textarea>') +
      '<div class="vform-foot tiny mute">' + ico("clock") + "<span>" + h(t("video.no_timeout_note")) + "</span></div>" +
      '<div class="vform-foot tiny mute">' + ico("code") + "<span>" + h(t("video.api_hint")) + "</span></div>" +
    "</form>" +
    '<div class="vupload">' + ico("arrowUp") +
      '<div><b>' + h(t("video.upload")) + "</b><small>" + h(t("video.upload_hint")) + "</small></div>" +
      '<input type="file" id="videoFile" accept="video/*" hidden>' +
      '<button type="button" class="btn ghost sm" data-act="video.pick">' + h(t("video.upload")) + "</button>" +
    "</div>";

  view.innerHTML =
    pageHead(t("video.title"), t("video.subtitle"),
      '<span class="chip ' + (ff ? "ok" : "warn") + '">' + ico("video") + h(t(ff ? "video.ffmpeg_on" : "video.ffmpeg_off")) + "</span>" +
      '<button class="btn ghost" data-act="video.refresh">' + ico("activity") + "<span>" + h(t("common.refresh")) + "</span></button>") +
    '<div class="vgrid2">' +
      '<div class="stack">' + card(t("video.manual"), "play", '<div id="videoProgBox">' + videoProgressHtml() + "</div>" + manualCard) + "</div>" +
      '<div class="stack">' + card(t("video.smart"), "zap", smartCard) + "</div>" +
    "</div>" +
    '<section class="card"><div class="card-head"><h2>' + ico("list") + h(t("video.history")) +
      '<span class="chip">' + num((state.video.jobs || []).length) + "</span></h2></div>" +
      '<div class="card-body" id="videoHistoryBody">' + videoJobsHtml() + "</div></section>";

  const file = $("#videoFile");
  if (file) file.addEventListener("change", () => uploadVideo());
}

async function loadVideo() {
  const [st, jobs] = await Promise.all([api("/api/video/status"), api("/api/video/analyses?limit=30")]);
  state.video.status = st;
  state.video.settings = st.settings || {};
  state.video.jobs = jobs.analyses || [];
}

async function pollVideoData() {
  const [st, jobs] = await Promise.all([api("/api/video/status"), api("/api/video/analyses?limit=30&brief=true")]);
  const previous = new Map((state.video.jobs || []).map((j) => [j.id, j]));
  const rows = await Promise.all((jobs.analyses || []).map(async (j) => {
    const old = previous.get(j.id);
    if (j.status === "done" && (!old || old.status !== "done" || !old.report)) {
      return (await api("/api/video/jobs/" + j.id)).job;
    }
    return { ...j, report: old && old.status === j.status ? old.report : {} };
  }));
  state.video.status = st;
  state.video.settings = st.settings || {};
  state.video.jobs = rows;
}

function pollVideo() {
  if (state.video.polling) return;
  let misses = 0, busy = false;
  state.video.polling = setInterval(async () => {
    if (state.route !== "video") { clearInterval(state.video.polling); state.video.polling = null; return; }
    if (busy) return;
    busy = true;
    try {
      const before = (state.video.jobs || []).filter((j) => j.status === "running").length;
      await pollVideoData();
      // Only touch the DOM when the markup really changed — otherwise a
      // click could land on a node that is being replaced mid-interaction.
      const history = $("#videoHistoryBody");
      const html = videoJobsHtml();
      if (history && history.dataset.sig !== html) {
        history.innerHTML = html;
        history.dataset.sig = html;
      }
      const progBox = $("#videoProgBox");
      const progHtml = videoProgressHtml();
      if (progBox && progBox.dataset.sig !== progHtml) {
        progBox.innerHTML = progHtml;
        progBox.dataset.sig = progHtml;
      }
      const running = (state.video.jobs || []).filter((j) => j.status === "running").length;
      if (!running && before) {
        clearInterval(state.video.polling); state.video.polling = null;
        toast(t("video.status_done"), "ok");
      }
      misses = 0;
    } catch (e) {
      misses += 1;
      if (misses > 5) { clearInterval(state.video.polling); state.video.polling = null; }
    } finally {
      busy = false;
    }
  }, 2500);
}

async function uploadVideo() {
  const input = $("#videoFile");
  const file = input && input.files && input.files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024 * 1024) { toast(t("err.file_too_large"), "err"); return; }
  toast(t("video.analysing"), "info");
  try {
    const res = await fetch("/api/video/upload?filename=" + encodeURIComponent(file.name), {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/octet-stream" }, body: file,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && data.detail) || "unknown");
    state.video.activeJob = data.job_id;
    await loadVideo(); await render(); pollVideo();
  } catch (e) { toast(errMsg(e), "err"); }
}

  /* wiring: page, actions and the form all register through the shared API */
  window.Rozaneh.registerPage("video", renderVideo);
  Object.assign(window.Rozaneh.actions, {
    "video.smart": async (el) => {
      const on = !el.classList.contains("on");
      try {
        const r = await api("/api/video/settings", { method: "PATCH", body: { smart: on } });
        state.video.settings = r.settings;
        toast(t("video.smart") + " · " + (on ? "ON" : "OFF"), "ok");
        await reload();
      } catch (e) { toast(errMsg(e), "err"); }
    },
    "video.interval": async (el) => {
      try {
        const r = await api("/api/video/settings", { method: "PATCH", body: { interval_minutes: Number(el.value) } });
        state.video.settings = r.settings;
      } catch (e) { toast(errMsg(e), "err"); }
    },
    "video.toggle": (el) => {
      const id = Number(el.dataset.id);
      state.video.activeJob = state.video.activeJob === id ? null : id;
      reload();
    },
    "video.refresh": async () => { await loadVideo(); await reload(); },
    "video.aiTest": async () => {
      try { await api("/api/video/ai-test", { method: "POST" }); toast(t("video.ai_test_ok"), "ok"); }
      catch (e) { toast(errMsg(e), "err"); }
    },
    "video.aiClear": () => confirmModal(t("video.ai_clear_confirm"), async () => {
      const cfg = state.video.settings || {};
      await api("/api/video/ai-settings", { method: "PUT", body: {
        base_url: cfg.ai_base_url, model: cfg.ai_model, vision: !!cfg.ai_vision, clear_key: true,
      }});
      toast(t("video.ai_cleared"), "ok"); await reload();
    }),
    "video.cancel": async (el) => {
      try {
        await api("/api/video/cancel/" + el.dataset.id, { method: "POST" });
        toast(t("video.cancel"), "ok");
        await loadVideo(); await reload();
      } catch (e) { toast(errMsg(e), "err"); }
    },
    "video.delete": async (el) => {
      confirmModal(t("video.delete") + "؟", async () => {
        await api("/api/video/analyses/" + el.dataset.id, { method: "DELETE" });
        await loadVideo(); await reload();
      });
    },
    "video.pick": () => { const f = $("#videoFile"); if (f) f.click(); },
    "video.copy": async () => {
      const pre = $("#videoCaption");
      const txt = pre ? pre.textContent : "";
      try {
        if (navigator.clipboard) await navigator.clipboard.writeText(txt);
        else {
          const ta = document.createElement("textarea");
          ta.value = txt; document.body.appendChild(ta); ta.select();
          document.execCommand("copy"); ta.remove();
        }
        toast(t("video.copied"), "ok");
      } catch (e) { toast(errMsg(e), "err"); }
    },
  });
  window.Rozaneh.forms["video-ai-settings"] = async (form) => {
    const v = fd(form);
    try {
      const r = await api("/api/video/ai-settings", { method: "PUT", body: {
        base_url: v.base_url, model: v.model, api_key: v.api_key || "", vision: !!form.elements.vision.checked,
      }});
      state.video.settings = r.settings;
      form.elements.api_key.value = "";
      toast(t("video.ai_saved"), "ok"); await reload();
    } catch (e) { toast(errMsg(e), "err"); }
  };
  window.Rozaneh.forms["video-quick"] = async (form) => {
    const v = fd(form);
    try {
      const parsed = new URL(v.url);
      const host = parsed.hostname.toLowerCase();
      const id = host.endsWith('youtu.be') ? parsed.pathname.slice(1).split('/')[0] :
        parsed.pathname === '/watch' ? parsed.searchParams.get('v') :
        (parsed.pathname.match(/^\/(?:shorts|embed|live)\/([\w-]+)/) || [])[1];
      if (!['http:', 'https:'].includes(parsed.protocol) || !['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'].includes(host) || !/^[\w-]{6,20}$/.test(id || '')) {
        toast(t("video.quick_invalid"), "err"); return;
      }
      const r = await api("/api/video/analyze", { method: "POST", body: { url: v.url, mode: "manual" } });
      form.reset(); state.video.activeJob = r.job_id;
      await reload(); pollVideo(); toast(t("video.analysing"), "info");
    } catch (e) { toast(errMsg(e), "err"); }
  };
  window.Rozaneh.forms.video = async (form) => {
    const v = fd(form);
    const payload = { url: v.url || "", title: v.title || "", transcript: v.transcript || "", mode: "manual" };
    if (!payload.url && !payload.transcript) { toast(t("err.url_required"), "err"); return; }
    try {
      const r = await api("/api/video/analyze", { method: "POST", body: payload });
      form.reset();
      state.video.activeJob = r.job_id;
      await reload();
      pollVideo();
      toast(t("video.analysing"), "info");
    } catch (e) { toast(errMsg(e), "err"); }
  };
})();

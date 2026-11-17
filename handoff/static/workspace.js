"use strict";

const byId = (id) => document.getElementById(id);
let token = "";
const status = (message) => { byId("status").textContent = message; };
let gptName = "GPT";
const nameOf = (model) => model === "gpt" ? gptName : "Gemini";

async function api(path, data) {
  const request = {
    headers: { Authorization: "Bearer " + token },
    cache: "no-store",
  };
  if (data !== undefined) {
    request.method = "POST";
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(data);
  }
  const response = await fetch("/api/" + path, request);
  if (response.status === 401) throw new Error("The workspace token was rejected.");
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}

function addOutput(container, title, text, collapsed) {
  const block = document.createElement(collapsed ? "details" : "div");
  if (!collapsed) block.className = "final";
  const label = document.createElement(collapsed ? "summary" : "h3");
  label.textContent = title;
  const output = document.createElement("pre");
  output.textContent = text;
  block.append(label, output);
  container.append(block);
}


const labels = { cross: "Cross-model review", same: "Same-model review", single: "Single pass" };
const examples = {
  csv: {
    route: "GPT → Gemini → GPT",
    evidence: "Task: Can we report a customer satisfaction average?\nCSV tool result: 4 rows. Scores: 5, 4, blank, 3. One score is missing.",
    draft: "The average is 4.0. Report this as the overall satisfaction score.",
    review: "4.0 is the mean of the three recorded responses. The fourth response is missing and could change the overall mean.",
    final: "The three recorded scores average 4.0 out of 5. One of four scores is missing (25%). Report this as the observed average and disclose the missing response.",
  },
  pdf: {
    route: "Gemini → GPT → Gemini",
    evidence: "Task: Summarize the refund policy.\n[Page 2] Standard purchases may be refunded within 30 days.\n[Page 5] Gift cards are excluded.",
    draft: "Customers can request a refund within 30 days.",
    review: "Page 2 covers standard purchases. Page 5 explicitly excludes gift cards.",
    final: "Standard purchases are eligible for a refund within 30 days [Page 2]. Gift cards are excluded [Page 5].",
  },
};

function showExample() {
  const example = examples[byId("example").value];
  const container = byId("demo-steps");
  container.replaceChildren();
  byId("demo-sequence").textContent = example.route;
  addOutput(container, "Task and evidence", example.evidence, true);
  addOutput(container, "Lead draft", example.draft, true);
  addOutput(container, "Reviewer findings", example.review, true);
  addOutput(container, "Revised answer", example.final, false);
}

byId("example").onchange = showExample;
showExample();

function exportRun(run) {
  const blob = new Blob([JSON.stringify(run, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const download = document.createElement("a");
  download.href = url;
  download.download = "handoff-" + run.id + ".json";
  download.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function showRun(run) {
  const detail = byId("details");
  detail.replaceChildren();
  const task = document.createElement("p");
  task.textContent = run.task + " · " + labels[run.mode || "cross"] + " · " + run.status;
  detail.append(task);
  if (run.offline) {
    const fixture = document.createElement("p");
    fixture.textContent = "Offline fixtures. No provider calls were made.";
    detail.append(fixture);
  }
  const exportButton = document.createElement("button");
  exportButton.textContent = "Export run JSON";
  exportButton.onclick = () => exportRun(run);
  detail.append(exportButton);
  if (run.steps.final) {
    const final = run.steps.final;
    addOutput(detail, "Final answer · " + final.model + " · " + final.latency_ms + " ms", final.text, false);
  }
  for (const key of ["draft", "review"]) {
    const step = run.steps[key];
    if (step) addOutput(detail, key + " · " + step.model + " · " + step.latency_ms + " ms", step.text, true);
  }
  if (run.context) addOutput(detail, "Shared context", run.context, true);
  if (run.status === "failed") {
    const error = document.createElement("p");
    error.textContent = "Failed: " + run.error.replaceAll("_", " ");
    const retry = document.createElement("button");
    retry.textContent = "Retry remaining steps";
    retry.onclick = async () => {
      if (!window.confirm("Retry using the saved context? A previous request may already have been charged.")) return;
      retry.disabled = true;
      await act(async () => {
        await history();
        showRun(await api("runs/" + run.id + "/retry", { approved: true }));
        await history();
      }, "Retrying…");
      retry.disabled = false;
    };
    detail.append(error, retry);
  }
}

async function history() {
  const data = await api("runs");
  const label = data.gpt_backend === "groq" ? "GPT-OSS via Groq" : "GPT";
  if (label !== gptName) {
    gptName = label;
    byId("lead").querySelector('option[value="gpt"]').textContent = gptName;
    updateSequence();
  }
  byId("mode").textContent = data.offline ? "Offline fixtures" : "Live providers";
  const list = byId("history");
  list.replaceChildren();
  if (!data.runs.length) {
    const empty = document.createElement("li");
    empty.textContent = "No handoffs yet.";
    list.append(empty);
  }
  for (const run of data.runs) {
    const item = document.createElement("li");
    const open = document.createElement("button");
    open.textContent = nameOf(run.lead) + " · " + labels[run.mode || "cross"] + " · " + run.task.slice(0, 110) + " · " + run.status;
    open.onclick = () => act(async () => showRun(await api("runs/" + run.id)));
    item.append(open);
    list.append(item);
  }
}

async function act(action, pending = "") {
  status(pending);
  try {
    await action();
    if (byId("status").textContent === pending) status("");
  } catch (error) {
    status(error.message);
  }
}

byId("unlock").onsubmit = (event) => {
  event.preventDefault();
  token = byId("token").value;
  act(async () => {
    await history();
    byId("token").value = "";
    byId("unlock").hidden = true;
    byId("public-demo").hidden = true;
    byId("workspace").hidden = false;
    byId("lock").hidden = false;
    byId("lead").focus();
  }, "Opening workspace…");
};
byId("lock").onclick = () => location.reload();

function updateSequence() {
  const lead = byId("lead").value;
  const mode = byId("review-mode").value;
  const name = nameOf(lead);
  const reviewer = mode === "same" ? name : nameOf(lead === "gpt" ? "gemini" : "gpt");
  byId("sequence").textContent = mode === "single" ? name : [name, reviewer, name].join(" → ");
  byId("approval-text").textContent = "I approve sharing this task and context with " +
    (mode === "cross" ? nameOf("gpt") + " and Gemini." : name + ".");
  byId("approve").checked = false;
}
byId("lead").onchange = updateSequence;
byId("review-mode").onchange = updateSequence;
updateSequence();

byId("file").onchange = () => {
  const file = byId("file").files[0];
  if (!file) return;
  byId("submit").disabled = true;
  act(async () => {
    if (file.size > 2 * 1024 * 1024) throw new Error("Upload limit is 2 MiB per file.");
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    const result = await api("prepare", { name: file.name, content: btoa(binary) });
    byId("context").value = result.context;
    byId("approve").checked = false;
    status(result.truncated ? "Preview was shortened. Check the context before sharing." : "Review the extracted context before sharing.");
  }, "Preparing your file locally on the server…").finally(() => { byId("submit").disabled = false; });
};

byId("run").oninput = (event) => {
  if (event.target.id !== "approve") byId("approve").checked = false;
};

byId("run").onsubmit = (event) => {
  event.preventDefault();
  byId("submit").disabled = true;
  byId("file").disabled = true;
  act(async () => {
    const result = await api("runs", {
      lead: byId("lead").value, mode: byId("review-mode").value, task: byId("task").value,
      context: byId("context").value, approved: byId("approve").checked,
    });
    showRun(result);
    await history();
    byId("approve").checked = false;
  }, "Running the selected workflow…").finally(() => {
    byId("submit").disabled = false;
    byId("file").disabled = false;
  });
};

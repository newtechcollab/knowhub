let notebookId = localStorage.getItem("notebook_id") || "";
let indexedSources =
  JSON.parse(localStorage.getItem("indexed_sources") || "[]");

let generatedSummaryItems =
  JSON.parse(localStorage.getItem("generated_summary_items") || "[]");

let generatedQuizItems =
  JSON.parse(localStorage.getItem("generated_quiz_items") || "[]");

generatedQuizItems = generatedQuizItems.filter(
  (item) => item.content && item.content !== "Generating quiz..."
);

localStorage.setItem(
  "generated_quiz_items",
  JSON.stringify(generatedQuizItems)
);

let generatedAudioItems =
  JSON.parse(localStorage.getItem("generated_audio_items") || "[]");

let publishedSummariesByRole =
  JSON.parse(localStorage.getItem("published_summaries_by_role") || "{}");

let publishedQuizzesByRole =
  JSON.parse(localStorage.getItem("published_quizzes_by_role") || "{}");

let publishedAudioByRole =
  JSON.parse(localStorage.getItem("published_audio_by_role") || "{}");

let selectedRole = localStorage.getItem("selected_role");

const addFolderBtn = document.getElementById("addFolderBtn");
const directoryInput = document.getElementById("directoryInput");

const loadingOverlay = document.getElementById("loadingOverlay");

const updateSourceFile = document.getElementById("updateSourceFile");

const currentRoleEl = document.getElementById("currentRole");
const changeRoleBtn = document.getElementById("changeRoleBtn");

const notebookIdEl = document.getElementById("notebookId");
const sourcesListEl = document.getElementById("sourcesList");

const uploadForm = document.getElementById("uploadForm");
const sourceFile = document.getElementById("sourceFile");

const urlForm = document.getElementById("urlForm");
const confleunceForm = document.getElementById("confluenceForm");
const websiteUrl = document.getElementById("websiteUrl");

const chatForm = document.getElementById("chatForm");
const questionInput = document.getElementById("question");
const chatBox = document.getElementById("chatBox");

const summaryBtn = document.getElementById("summaryBtn");
const quizBtn = document.getElementById("quizBtn");
const audioBtn = document.getElementById("audioBtn");
const newNotebookBtn = document.getElementById("newNotebookBtn");

const summaryOutput = document.getElementById("summaryOutput");
const quizOutput = document.getElementById("quizOutput");
const audioScript = document.getElementById("audioScript");
const audioPlayer = document.getElementById("audioPlayer");

const tabButtons = document.querySelectorAll(".tab-btn");
const tabContents = document.querySelectorAll(".tab-content");

const promptModal = document.getElementById("promptModal");
const modalTitle = document.getElementById("modalTitle");
const modalDescription = document.getElementById("modalDescription");
const modalPrompt = document.getElementById("modalPrompt");
const closeModalBtn = document.getElementById("closeModalBtn");
const cancelModalBtn = document.getElementById("cancelModalBtn");
const submitModalBtn = document.getElementById("submitModalBtn");

let activeAction = null;

const SUPPORTED_FOLDER_EXTENSIONS = [
  ".pdf",
  ".txt",
  ".docx",
  ".pptx",
  ".xlsx",
];

const roleSelect = document.getElementById("roleSelect");

function isSupportedFolderFile(file) {
  const name = (file.name || "").toLowerCase();
  const path = (file.webkitRelativePath || file.name || "").toLowerCase();

  if (!name) return false;

  // Skip macOS / system / temp files
  if (
    name.startsWith(".") ||
    name.startsWith("~$") ||
    name === ".ds_store" ||
    name === ".localized"
  ) {
    return false;
  }

  // Skip hidden folders/files in the path
  const pathParts = path.split("/").filter(Boolean);
  if (pathParts.some((part) => part.startsWith(".") || part.startsWith("~$"))) {
    return false;
  }

  return SUPPORTED_FOLDER_EXTENSIONS.some((ext) => name.endsWith(ext));
}

if (addFolderBtn && directoryInput) {
  addFolderBtn.addEventListener("click", () => {
    directoryInput.click();
  });

  directoryInput.addEventListener("change", async () => {
    if (!directoryInput.files || !directoryInput.files.length) return;

    const allFiles = Array.from(directoryInput.files);
    const allowedFiles = allFiles.filter(isSupportedFolderFile);
    const skippedFiles = allFiles.length - allowedFiles.length;

    if (!allowedFiles.length) {
      showError(
        "No supported files were found in that folder. Please include PDF, TXT, DOCX, PPTX, or XLSX files."
      );
      directoryInput.value = "";
      return;
    }

    const formData = new FormData();

    if (notebookId) {
      formData.append("notebook_id", notebookId);
    }

    allowedFiles.forEach((file) => {
      formData.append(
        "files",
        file,
        file.webkitRelativePath || file.name
      );
    });

    showUploadStatus(
      `Uploading ${allowedFiles.length} files${skippedFiles ? ` (${skippedFiles} skipped)` : ""}...`
    );

    try {
      const res = await fetch("/ingest-directory", {
        method: "POST",
        body: formData,
      });

      let data = {};
      try {
        data = await res.json();
      } catch {
        data = { error: `Folder upload failed with status ${res.status}.` };
      }

      if (!res.ok || data.error) {
        showError(data.error || "Folder upload failed.");
        return;
      }

      if (data.notebook_id) {
        setNotebookId(data.notebook_id);
      }

      showUploadStatus("Folder indexing started in background...");

      const job = await pollJobStatus(data.job_id);

      if (job.status !== "done") {
        showError(job.error || job.message || "Folder indexing failed.");
        return;
      }

      const result = job.result || {};

      appendMessage(
        "assistant",
        `Indexed folder "${result.source_name || data.source_name || "Folder"}" with ${result.files_indexed || 0} files and ${result.chunks_indexed || 0} chunks.`
      );

      showToast(
        `Folder indexed successfully: ${result.files_indexed || 0} files.`
      );

      const folderSource = {
        source_id: result.source_id || `folder-${data.job_id}`,
        source_name: result.source_name || data.source_name || "Folder",
        source_type: "folder",
        file_count: result.files_indexed || 0,
      };

      // If this folder was already added, update it instead of duplicating it.
      const existingIndex = indexedSources.findIndex(
        (s) =>
          typeof s === "object" &&
          s.source_type === "folder" &&
          s.source_id === folderSource.source_id
      );

      if (existingIndex >= 0) {
        indexedSources[existingIndex] = folderSource;
        saveIndexedSources();
        renderSources();
      } else {
        appendSource(folderSource);
      }

    } catch (error) {
      console.error("Folder upload failed:", error);
      showError("Folder upload failed. Please try again.");
    } finally {
      hideUploadStatus();
      directoryInput.value = "";
    }
  });
}

const defaultPrompts = {
  summary:
    "Summarize this notebook for a senior executive in 5 concise bullet points. Include key themes, risks, opportunities, and recommended next steps.",

  quiz:
    "Create a 5-question multiple-choice quiz from this notebook. Include 4 options for each question, the correct answer, and a short explanation.",

  audio:
    "Create a natural 2-3 minute spoken audio overview of this notebook. Make it clear, engaging, and suitable for a business audience.",
};

function showUploadStatus(message) {
  let status = document.getElementById("uploadStatusChip");

  if (!status) {
    status = document.createElement("div");
    status.id = "uploadStatusChip";
    status.className = "upload-status-chip";
    status.innerHTML = `
      <span class="upload-status-dot"></span>
      <span class="upload-status-text"></span>
    `;
    document.body.appendChild(status);
  }

  const textEl = status.querySelector(".upload-status-text");
  if (textEl) {
    textEl.textContent = message;
  }

  status.classList.add("show");
}

function hideUploadStatus() {
  const status = document.getElementById("uploadStatusChip");
  if (status) {
    status.classList.remove("show");
  }
}

function showChatLoading() {
  const existing = document.getElementById("chatLoadingIndicator");

  if (existing) {
    return;
  }

  const wrapper = document.createElement("div");
  wrapper.id = "chatLoadingIndicator";
  wrapper.className = "message assistant";

  wrapper.innerHTML = `
    <div class="message-body chat-loading-body">
      <span class="chat-loading-text">Thinking</span>

      <span class="chat-loading-dots">
        <span></span>
        <span></span>
        <span></span>
      </span>
    </div>
  `;

  chatBox.appendChild(wrapper);
  chatBox.scrollTop = chatBox.scrollHeight;
}


function hideChatLoading() {
  const indicator = document.getElementById("chatLoadingIndicator");

  if (indicator) {
    indicator.remove();
  }
}

function showToast(message) {
  let toast = document.getElementById("appToast");

  if (!toast) {
    toast = document.createElement("div");
    toast.id = "appToast";
    toast.className = "app-toast";
    document.body.appendChild(toast);
  }

  toast.textContent = message;
  toast.classList.add("show");

  window.clearTimeout(window.__appToastTimer);
  window.__appToastTimer = window.setTimeout(() => {
    toast.classList.remove("show");
  }, 3500);
}

async function pollJobStatus(jobId) {
  const maxAttempts = 180;
  let attempts = 0;

  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      attempts += 1;

      try {
        const res = await fetch(`/jobs/${jobId}`);
        const data = await res.json();

        if (data.error) {
          clearInterval(timer);
          reject(new Error(data.error));
          return;
        }

        if (data.status === "done") {
          clearInterval(timer);
          resolve(data);
          return;
        }

        if (data.status === "failed") {
          clearInterval(timer);
          reject(new Error(data.error || data.message || "Folder indexing failed."));
          return;
        }

        if (attempts >= maxAttempts) {
          clearInterval(timer);
          reject(new Error("Folder indexing timed out."));
        }
      } catch (error) {
        clearInterval(timer);
        reject(error);
      }
    }, 2000);
  });
}

function setChatFormDisabled(disabled) {
  if (questionInput) {
    questionInput.disabled = disabled;
  }

  const submitButton = chatForm?.querySelector('button[type="submit"]');

  if (submitButton) {
    submitButton.disabled = disabled;
  }
}

function showError(message) {
  alert(message || "Something went wrong. Please try again.");
}

function showLoading(title = "Processing", text = "Please wait...") {
  if (!loadingOverlay) return;

  const titleEl = loadingOverlay.querySelector(".loading-title");
  const textEl = loadingOverlay.querySelector(".loading-text");

  if (titleEl) titleEl.textContent = title;
  if (textEl) textEl.textContent = text;

  loadingOverlay.classList.remove("hidden");
}

function hideLoading() {
  if (!loadingOverlay) return;
  loadingOverlay.classList.add("hidden");
}

function saveIndexedSources() {
  localStorage.setItem("indexed_sources", JSON.stringify(indexedSources));
}

function saveGeneratedSummaryItems() {
  localStorage.setItem(
    "generated_summary_items",
    JSON.stringify(generatedSummaryItems)
  );
}

function saveGeneratedQuizItems() {
  localStorage.setItem(
    "generated_quiz_items",
    JSON.stringify(generatedQuizItems)
  );
}

function saveGeneratedAudioItems() {
  localStorage.setItem(
    "generated_audio_items",
    JSON.stringify(generatedAudioItems)
  );
}

function savePublishedSummaries() {
  localStorage.setItem(
    "published_summaries_by_role",
    JSON.stringify(publishedSummariesByRole)
  );
}

function savePublishedQuizzes() {
  localStorage.setItem(
    "published_quizzes_by_role",
    JSON.stringify(publishedQuizzesByRole)
  );
}

function savePublishedAudio() {
  localStorage.setItem(
    "published_audio_by_role",
    JSON.stringify(publishedAudioByRole)
  );
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function getPreview(text) {
  const safeText = String(text || "").trim();

  if (!safeText) {
    return "No content available.";
  }

  const lines = safeText
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  return lines.slice(0, 2).join("\n") || safeText.slice(0, 220);
}

function applyRolePermissions() {
  if (!selectedRole) {
    window.location.href = "/";
    return;
  }

  if (currentRoleEl) {
    currentRoleEl.textContent = selectedRole;
  }

  const isAdmin = selectedRole === "Admin";
  const adminOnlyItems = document.querySelectorAll(".admin-only");

  adminOnlyItems.forEach((item) => {
    item.style.display = isAdmin ? "" : "none";
  });

  const welcomeCard = document.querySelector(".welcome-card");

  if (welcomeCard && !isAdmin) {
    welcomeCard.remove();
  }

  renderSummaryItems();
  renderQuizItems();
  renderAudioItems();
}

function setNotebookId(id) {
  notebookId = id;
  localStorage.setItem("notebook_id", id);
  notebookIdEl.textContent = id || "Not created yet";
}

function renderSources() {
  if (!sourcesListEl) return;

  if (!indexedSources.length) {
    sourcesListEl.className = "sources-list empty-state";
    sourcesListEl.textContent = "No sources yet";
    return;
  }

  sourcesListEl.className = "sources-list";

  sourcesListEl.innerHTML = indexedSources
    .map((source) => {
      if (typeof source === "string") {
        return `
          <div class="source-item">
            <div class="source-info">
              <div class="source-name">
                ${escapeHtml(source)}
              </div>
            </div>
          </div>
        `;
      }

      const sourceType =
        String(source.source_type || "").toLowerCase();

      if (sourceType === "folder") {
        return `
          <div class="source-item folder-source-item">
            <div class="source-info">

              <div class="source-name">
                📁 ${escapeHtml(
                  source.source_name || "Folder"
                )}
              </div>

              <div class="source-type">
                ${Number(source.file_count || 0)} files
              </div>

            </div>
          </div>
        `;
      }

      return `
        <div class="source-item">
          <div class="source-info">

            <div class="source-name">
              ${escapeHtml(
                source.source_name || "Source"
              )}
            </div>

            <div class="source-type">
              ${escapeHtml(
                source.source_type || ""
              )}
            </div>

          </div>
        </div>
      `;
    })
    .join("");
}

function appendSource(name) {
  indexedSources.push(name);
  saveIndexedSources();
  renderSources();
}

function appendMessage(role, text, citations = []) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;

  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;
  wrapper.appendChild(body);

  if (citations.length) {
    const sourcesLink = document.createElement("button");
    sourcesLink.className = "sources-toggle-link";
    sourcesLink.textContent = "Show Sources";

    const citeBox = document.createElement("div");
    citeBox.className = "citations hidden";

    citations.forEach((c) => {
      const item = document.createElement("div");
      item.className = "citation";
      item.innerHTML = `<strong>${escapeHtml(c.source_name)}</strong> · page ${escapeHtml(
        c.page
      )}<br>${escapeHtml(c.snippet)}`;
      citeBox.appendChild(item);
    });

    sourcesLink.addEventListener("click", () => {
      const isHidden = citeBox.classList.contains("hidden");

      if (isHidden) {
        citeBox.classList.remove("hidden");
        sourcesLink.textContent = "Hide Sources";
      } else {
        citeBox.classList.add("hidden");
        sourcesLink.textContent = "Show Sources";
      }
    });

    wrapper.appendChild(sourcesLink);
    wrapper.appendChild(citeBox);
  }

  chatBox.appendChild(wrapper);
  chatBox.scrollTop = chatBox.scrollHeight;
}

function setActiveTab(targetId) {
  tabButtons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.target === targetId);
  });

  tabContents.forEach((content) => {
    content.classList.toggle("active", content.id === targetId);
  });
}

function attachExpandHandlers() {
  document.querySelectorAll(".expand-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const type = btn.dataset.type;
      const index = btn.dataset.index;

      const preview = document.getElementById(`${type}Preview-${index}`);
      const full = document.getElementById(`${type}Full-${index}`);

      if (!preview || !full) return;

      const isExpanded = !full.classList.contains("hidden");

      if (isExpanded) {
        full.classList.add("hidden");
        preview.classList.remove("hidden");
        btn.textContent = "Expand";
      } else {
        preview.classList.add("hidden");
        full.classList.remove("hidden");
        btn.textContent = "Collapse";
      }
    });
  });
}

function renderSummaryItems() {
  const tab = document.getElementById("summaryTab");

  let list = document.getElementById("summaryList");
  if (!list) {
    list = document.createElement("div");
    list.id = "summaryList";
    list.className = "generated-list";
    tab.querySelector(".output-card").appendChild(list);
  }

  const isAdmin = selectedRole === "Admin";

  if (!isAdmin) {
    const roleSummaries = publishedSummariesByRole[selectedRole] || [];

    list.innerHTML = roleSummaries.length
      ? roleSummaries
          .map(
            (item, index) => `
              <div class="generated-item">
                <div class="generated-item-header">
                  <strong>${escapeHtml(item.title)}</strong>
                  <span>${escapeHtml(item.createdAt)}</span>
                </div>

                <pre class="output-text preview-text" id="summaryPreview-${index}">${escapeHtml(
                  getPreview(item.content)
                )}</pre>
                <pre class="output-text full-text hidden" id="summaryFull-${index}">${escapeHtml(
                  item.content
                )}</pre>

                <button class="secondary-light-btn expand-btn" data-type="summary" data-index="${index}">
                  Expand
                </button>
              </div>
            `
          )
          .join("")
      : `<div class="empty-state">No summaries have been published for ${escapeHtml(
          selectedRole
        )}.</div>`;

    attachExpandHandlers();
    return;
  }

  list.innerHTML = generatedSummaryItems
    .map((item, index) => {
      const isGenerating =
        item.content && String(item.content).startsWith("Generating");

      return `
        <div class="generated-item">
          <div class="generated-item-header">
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml(item.createdAt)}</span>
          </div>

          <pre class="output-text preview-text" id="summaryAdminPreview-${index}">${escapeHtml(
            getPreview(item.content)
          )}</pre>

          <pre class="output-text full-text hidden" id="summaryAdminFull-${index}">${escapeHtml(
            item.content
          )}</pre>

          ${
            isGenerating
              ? ""
              : `
                <button class="secondary-light-btn expand-btn" data-type="summaryAdmin" data-index="${index}">
                  Expand
                </button>

                <div class="publish-row">
                  <select class="publish-role-select" data-index="${index}">
                    <option value="Developer">Developer</option>
                    <option value="Tester">Tester</option>
                    <option value="DevOps">DevOps</option>
                    <option value="Support">Support</option>
                  </select>

                  <button class="primary-btn publish-summary-btn" data-index="${index}">
                    Make available
                  </button>

                  <button
                    class="delete-btn delete-summary-btn"
                    data-index="${index}"
                    type="button"
                  >
                    Delete
                  </button>
                </div>
              `
          }
        </div>
      `;
    })
    .join("");

  attachExpandHandlers();

  document.querySelectorAll(".publish-summary-btn").forEach((btn) => {

    const index = Number(btn.dataset.index);
    const select = document.querySelector(
        `.publish-role-select[data-index="${index}"]`
    );

    function refreshButton() {

        const role = select.value;
        const item = generatedSummaryItems[index];

        const alreadyPublished =
            (publishedSummariesByRole[role] || []).some(
                s =>
                    s.title === item.title &&
                    s.content === item.content
            );

        if (alreadyPublished) {
            btn.textContent = `Already Published to ${role}`;
            btn.disabled = true;
        } else {
            btn.textContent = "Make available";
            btn.disabled = false;
        }
    }

    refreshButton();

    select.addEventListener("change", refreshButton);

    btn.addEventListener("click", () => {

        const role = select.value;
        const item = generatedSummaryItems[index];

        if (!publishedSummariesByRole[role]) {
            publishedSummariesByRole[role] = [];
        }

        publishedSummariesByRole[role].unshift({
            title: item.title,
            content: item.content,
            createdAt: new Date().toLocaleString(),
        });

        savePublishedSummaries();

        refreshButton();
    });
  });

  document.querySelectorAll(".delete-summary-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const index = Number(btn.dataset.index);
      const item = generatedSummaryItems[index];

      if (!item) return;

      const confirmed = window.confirm(
        `Delete "${item.title || `Summary ${index + 1}`}"?`
      );

      if (!confirmed) return;

      // Remove from generated summaries.
      generatedSummaryItems.splice(index, 1);

      // Also remove published copies from every role.
      Object.keys(publishedSummariesByRole).forEach((role) => {
        publishedSummariesByRole[role] =
          publishedSummariesByRole[role].filter((publishedItem) => {
            // Prefer output ID when available.
            if (item.outputId && publishedItem.outputId) {
              return publishedItem.outputId !== item.outputId;
            }

            // Compatibility with older items without IDs.
            return !(
              publishedItem.title === item.title &&
              publishedItem.content === item.content
            );
          });
      });

      saveGeneratedSummaryItems();
      savePublishedSummaries();

      renderSummaryItems();
    });
  });
}

function renderQuizItems() {
  const tab = document.getElementById("quizTab");

  let list = document.getElementById("quizList");
  if (!list) {
    list = document.createElement("div");
    list.id = "quizList";
    list.className = "generated-list";
    tab.querySelector(".output-card").appendChild(list);
  }

  const isAdmin = selectedRole === "Admin";

  if (!isAdmin) {
    const roleQuizzes = publishedQuizzesByRole[selectedRole] || [];

    list.innerHTML = roleQuizzes.length
      ? roleQuizzes
          .map(
            (item, index) => `
              <div class="generated-item">
                <div class="generated-item-header">
                  <strong>${escapeHtml(item.title || `Quiz ${index + 1}`)}</strong>
                  <span>${escapeHtml(item.createdAt)}</span>
                </div>

                <pre class="output-text preview-text" id="quizPreview-${index}">${escapeHtml(
                  getPreview(item.content)
                )}</pre>

                <pre class="output-text full-text hidden" id="quizFull-${index}">${escapeHtml(
                  item.content
                )}</pre>

                <button class="secondary-light-btn expand-btn" data-type="quiz" data-index="${index}">
                  Expand
                </button>
              </div>
            `
          )
          .join("")
      : `<div class="empty-state">No quizzes have been published for ${escapeHtml(
          selectedRole
        )}.</div>`;

    attachExpandHandlers();
    return;
  }

  list.innerHTML = generatedQuizItems.length
    ? generatedQuizItems
        .map((item, index) => {
          const isGenerating =
            item.content && String(item.content).startsWith("Generating");

          return `
            <div class="generated-item">
              <div class="generated-item-header">
                <strong>${escapeHtml(item.title || `Quiz Draft ${index + 1}`)}</strong>
                <span>${escapeHtml(item.createdAt)}</span>
              </div>

              <pre class="output-text preview-text" id="quizAdminPreview-${index}">${escapeHtml(
                getPreview(item.content)
              )}</pre>

              <pre class="output-text full-text hidden" id="quizAdminFull-${index}">${escapeHtml(
                item.content
              )}</pre>

              ${
                isGenerating
                  ? ""
                  : `
                    <button class="secondary-light-btn expand-btn" data-type="quizAdmin" data-index="${index}">
                      Expand
                    </button>

                    <div class="publish-row">
                      <select class="publish-quiz-role-select" data-index="${index}">
                        <option value="Developer">Developer</option>
                        <option value="Tester">Tester</option>
                        <option value="DevOps">DevOps</option>
                        <option value="Support">Support</option>
                      </select>

                      <button class="primary-btn publish-quiz-btn" data-index="${index}">
                        Make available
                      </button>
                    </div>
                  `
              }
            </div>
          `;
        })
        .join("")
    : `<div class="empty-state">No quizzes available.</div>`;

  attachExpandHandlers();

  document.querySelectorAll(".publish-quiz-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const index = Number(btn.dataset.index);
      const select = document.querySelector(
        `.publish-quiz-role-select[data-index="${index}"]`
      );

      if (!select || !generatedQuizItems[index]) return;

      const role = select.value;
      const item = generatedQuizItems[index];

      if (!publishedQuizzesByRole[role]) {
        publishedQuizzesByRole[role] = [];
      }

      publishedQuizzesByRole[role].unshift({
        title: item.title,
        content: item.content,
        createdAt: new Date().toLocaleString(),
      });

      savePublishedQuizzes();

      btn.textContent = `Published to ${role}`;
      btn.disabled = true;
    });
  });
}

function renderAudioItems() {
  const tab = document.getElementById("audioTab");

  let list = document.getElementById("audioList");
  if (!list) {
    list = document.createElement("div");
    list.id = "audioList";
    list.className = "generated-list";
    tab.querySelector(".output-card").appendChild(list);
  }

  const isAdmin = selectedRole === "Admin";

  if (!isAdmin) {
    const roleAudioItems = publishedAudioByRole[selectedRole] || [];

    list.innerHTML = roleAudioItems.length
      ? roleAudioItems
          .map(
            (item, index) => `
              <div class="generated-item">
                <div class="generated-item-header">
                  <strong>${escapeHtml(item.title || `Audio Overview ${index + 1}`)}</strong>
                  <span>${escapeHtml(item.createdAt)}</span>
                </div>

                ${
                  item.audioUrl
                    ? `<audio controls src="${escapeHtml(item.audioUrl)}" style="width:100%; margin:10px 0;"></audio>`
                    : ""
                }

                <pre class="output-text preview-text" id="audioPreview-${index}">${escapeHtml(
                  getPreview(item.script)
                )}</pre>

                <pre class="output-text full-text hidden" id="audioFull-${index}">${escapeHtml(
                  item.script
                )}</pre>

                <button class="secondary-light-btn expand-btn" data-type="audio" data-index="${index}">
                  Expand
                </button>
              </div>
            `
          )
          .join("")
      : `<div class="empty-state">No audio overviews have been published for ${escapeHtml(
          selectedRole
        )}.</div>`;

    attachExpandHandlers();
    return;
  }

  list.innerHTML = generatedAudioItems.length
    ? generatedAudioItems
        .map((item, index) => {
          const isGenerating =
            item.script && String(item.script).startsWith("Generating");

          return `
            <div class="generated-item">
              <div class="generated-item-header">
                <strong>${escapeHtml(item.title || `Audio Draft ${index + 1}`)}</strong>
                <span>${escapeHtml(item.createdAt)}</span>
              </div>

              ${
                item.audioUrl
                  ? `<audio controls src="${escapeHtml(item.audioUrl)}" style="width:100%; margin:10px 0;"></audio>`
                  : ""
              }

              <pre class="output-text preview-text" id="audioAdminPreview-${index}">${escapeHtml(
                getPreview(item.script)
              )}</pre>

              <pre class="output-text full-text hidden" id="audioAdminFull-${index}">${escapeHtml(
                item.script
              )}</pre>

              ${
                isGenerating
                  ? ""
                  : `
                    <button class="secondary-light-btn expand-btn" data-type="audioAdmin" data-index="${index}">
                      Expand
                    </button>

                    <div class="publish-row">
                      <select class="publish-audio-role-select" data-index="${index}">
                        <option value="Developer">Developer</option>
                        <option value="Tester">Tester</option>
                        <option value="DevOps">DevOps</option>
                        <option value="Support">Support</option>
                      </select>

                      <button class="primary-btn publish-audio-btn" data-index="${index}">
                        Make available
                      </button>

                      <button class="secondary-light-btn delete-audio-btn" data-index="${index}">
                        Delete
                      </button>
                    </div>
                  `
              }
            </div>
          `;
        })
        .join("")
    : `<div class="empty-state">No audio overviews available.</div>`;

  attachExpandHandlers();

  document.querySelectorAll(".publish-audio-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const index = Number(btn.dataset.index);
      const select = document.querySelector(
        `.publish-audio-role-select[data-index="${index}"]`
      );

      if (!select || !generatedAudioItems[index]) return;

      const role = select.value;
      const item = generatedAudioItems[index];

      if (!publishedAudioByRole[role]) {
        publishedAudioByRole[role] = [];
      }

      publishedAudioByRole[role].unshift({
        outputId: item.outputId || item.id,
        title: item.title,
        script: item.script,
        audioUrl: item.audioUrl
          ? `${item.audioUrl.split("?")[0]}?v=${Date.now()}`
          : "",
        createdAt: new Date().toLocaleString(),
      });

      savePublishedAudio();

      btn.textContent = `Published to ${role}`;
      btn.disabled = true;
    });
  });

  document.querySelectorAll(".delete-audio-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const index = Number(btn.dataset.index);
      const item = generatedAudioItems[index];

      if (!item) return;

      const confirmed = window.confirm(
        `Delete "${item.title || "this audio overview"}"? It will also be removed from all published roles.`
      );

      if (!confirmed) return;

      try {
        if (item.audioUrl) {
          await fetch("/delete-audio", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              audio_url: item.audioUrl.split("?")[0],
            }),
          });
        }
      } catch (error) {
        console.error("Unable to delete audio file:", error);
      }

      generatedAudioItems.splice(index, 1);

      Object.keys(publishedAudioByRole).forEach((role) => {
        publishedAudioByRole[role] = publishedAudioByRole[role].filter(
          (publishedItem) => {
            if (item.outputId && publishedItem.outputId) {
              return publishedItem.outputId !== item.outputId;
            }

            return !(
              publishedItem.audioUrl?.split("?")[0] ===
                item.audioUrl?.split("?")[0] &&
              publishedItem.title === item.title
            );
          }
        );
      });

      saveGeneratedAudioItems();
      savePublishedAudio();
      renderAudioItems();
    });
  });

}

function clearNotebook() {
  publishedQuizzesByRole = {};
  publishedSummariesByRole = {};
  publishedAudioByRole = {};

  localStorage.removeItem("published_audio_by_role");
  localStorage.removeItem("published_quizzes_by_role");
  localStorage.removeItem("published_summaries_by_role");
  localStorage.removeItem("generated_summary_items");
  localStorage.removeItem("generated_quiz_items");
  localStorage.removeItem("generated_audio_items");
  localStorage.removeItem("indexed_sources");

  notebookId = "";
  indexedSources = [];
  generatedSummaryItems = [];
  generatedQuizItems = [];
  generatedAudioItems = [];

  localStorage.removeItem("notebook_id");
  notebookIdEl.textContent = "Not created yet";

  renderSources();
  renderSummaryItems();
  renderQuizItems();
  renderAudioItems();

  if (selectedRole === "Admin") {
  chatBox.innerHTML = `
    <div class="welcome-card">
      <div class="welcome-title">Start with a source</div>
      <div class="welcome-text">
        Ask questions, review generated summaries, quizzes, and audio overviews.
        Admin users can add notebook sources and generate new outputs.
      </div>
    </div>
  `;
  } else {
    chatBox.innerHTML = "";
  }

  if (summaryOutput) {
    summaryOutput.textContent = "";
    summaryOutput.style.display = "none";
  }

  if (quizOutput) {
    quizOutput.textContent = "";
    quizOutput.style.display = "none";
  }

  if (audioScript) {
    audioScript.textContent = "";
    audioScript.style.display = "none";
  }

  if (audioPlayer) {
    audioPlayer.src = "";
    audioPlayer.style.display = "none";
  }
}

function openPromptModal(action) {
  activeAction = action;

  const titleInput = document.getElementById("contentTitle");

  if (titleInput) {
    titleInput.value = "";
    titleInput.placeholder = "";
  }

  modalPrompt.value = "";
  modalPrompt.placeholder = "";

  if (action === "summary") {
    modalTitle.textContent = "Generate Summary";
    modalDescription.textContent = "Customize how the summary should be written.";

    if (titleInput) {
      titleInput.placeholder = "Enter summary title";
    }

    modalPrompt.placeholder = defaultPrompts.summary;
  }

  if (action === "quiz") {
    modalTitle.textContent = "Generate Quiz";
    modalDescription.textContent =
      "Customize the quiz format, difficulty, and number of questions.";

    if (titleInput) {
      titleInput.placeholder = "Enter quiz title";
    }

    modalPrompt.placeholder = defaultPrompts.quiz;
  }

  if (action === "audio") {
    modalTitle.textContent = "Generate Audio Overview";
    modalDescription.textContent =
      "Customize the narration style, audience, and length.";

    if (titleInput) {
      titleInput.placeholder = "Enter audio overview title";
    }

    modalPrompt.placeholder = defaultPrompts.audio;
  }

  promptModal.classList.remove("hidden");

  if (titleInput) {
    titleInput.focus();
  } else {
    modalPrompt.focus();
  }
}

function closePromptModal() {
  promptModal.classList.add("hidden");
  activeAction = null;
  modalPrompt.value = "";
  modalPrompt.placeholder = "";
}

if (notebookId) {
  notebookIdEl.textContent = notebookId;
}

if (summaryOutput) {
  summaryOutput.textContent = "";
  summaryOutput.style.display = "none";
}

if (quizOutput) {
  quizOutput.textContent = "";
  quizOutput.style.display = "none";
}

if (audioScript) {
  audioScript.textContent = "";
  audioScript.style.display = "none";
}

renderSources();
renderSummaryItems();
renderQuizItems();
renderAudioItems();
applyRolePermissions();

if (changeRoleBtn) {
  changeRoleBtn.addEventListener("click", () => {
    window.location.href = "/";
  });
}

if (newNotebookBtn) {
  newNotebookBtn.addEventListener("click", () => {
    clearNotebook();
  });
}

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    setActiveTab(btn.dataset.target);
  });
});

if (closeModalBtn) closeModalBtn.addEventListener("click", closePromptModal);
if (cancelModalBtn) cancelModalBtn.addEventListener("click", closePromptModal);

if (promptModal) {
  promptModal.addEventListener("click", (e) => {
    if (e.target === promptModal) {
      closePromptModal();
    }
  });
}

document.addEventListener("keydown", (e) => {
  if (
    e.key === "Escape" &&
    promptModal &&
    !promptModal.classList.contains("hidden")
  ) {
    closePromptModal();
  }
});

if (uploadForm) {
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (selectedRole !== "Admin") {
      alert("Only Admin can upload sources.");
      return;
    }

    if (!sourceFile.files.length) return;

    const formData = new FormData();
    formData.append("file", sourceFile.files[0]);

    if (notebookId) {
      formData.append("notebook_id", notebookId);
    }

    showLoading("Uploading source", "Please wait while the file is uploaded, chunked, embedded, and indexed...");

    try {
      const res = await fetch("/upload", {
        method: "POST",
        body: formData,
      });

      const data = await res.json();

      if (!res.ok || data.error) {
        showError(data.error || "Source upload failed.");
        return;
      }

      setNotebookId(data.notebook_id);
      appendSource({ source_id: data.source_id, source_name: data.source_name, source_type: data.source_type, });

      appendMessage(
        "assistant",
        `Indexed ${data.source_type} source "${data.source_name}" with ${data.chunks_indexed} chunks across ${data.pages_indexed} page(s).`
      );

      sourceFile.value = "";
    } catch (err) {
      console.error("Upload failed:", err);
      showError("Upload failed. Please check the file and try again.");
    } finally {
      hideLoading();
    }
    });
}

if (urlForm) {
  urlForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (selectedRole !== "Admin") {
      alert("Only Admin can add website sources.");
      return;
    }

    const url = websiteUrl.value.trim();
    if (!url) return;

    showLoading("Adding website", "Please wait while the website or repository is read, chunked, embedded, and indexed...");

    try {
      const res = await fetch("/ingest-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url,
          notebook_id: notebookId || null,
        }),
      });

      let data = {};
      try {
          data = await res.json();
      } catch (jsonError) {
          data = {
          error: `Website indexing failed with status ${res.status}. Please try again.`,
        };
      }

      if (!res.ok || data.error) {
        showError(data.error || "Source upload failed.");
        return;
      }

      setNotebookId(data.notebook_id);
      appendSource({ source_id: data.source_id, source_name: data.source_name, source_type: "website", });

      appendMessage(
        "assistant",
        `Indexed website "${data.source_name}" from ${data.url} with ${data.chunks_indexed} chunks.`
      );

      websiteUrl.value = "";
    } catch (err) {
      console.error("Upload failed:", err);
      showError("Upload failed. Please check the file and try again.");
    } finally {
      hideLoading();
    }
    });
}

if (confleunceForm) {
  confleunceForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (selectedRole !== "Admin") {
      alert("Only Admin can add Confluence sources.");
      return;
    }

    const url = confUrl.value.trim();
    if (!url) return;

    showLoading("Adding Confluence", "Please wait while the Confluence Space/Page is read, chunked, embedded, and indexed...");

    try {
      const res = await fetch("/ingest-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url, 
          notebook_id: notebookId || null,
        }),
      });

      let data = {};
      try {
          data = await res.json();
      } catch (jsonError) {
          data = {
          error: `Confluence indexing failed with status ${res.status}. Please try again.`,
        };
      }

      if (!res.ok || data.error) {
        showError(data.error || "Source upload failed.");
        return;
      }

      setNotebookId(data.notebook_id);
      appendSource({ source_id: data.source_id, source_name: data.source_name, source_type: "website", });

      appendMessage(
        "assistant",
        `Indexed Confluence "${data.source_name}" from ${data.url} with ${data.chunks_indexed} chunks.`
      );

      confUrl.value = "";
      } catch (err) {
        console.error("Upload failed:", err);
        showError("Upload failed. Please check Confluence URL and try again.");
      } finally {
      hideLoading();
      }
    });
}

if (updateSourceFile) {
  updateSourceFile.addEventListener("change", async () => {
    if (selectedRole !== "Admin") {
      alert("Only Admin can update sources.");
      return;
    }

    const index = Number(updateSourceFile.dataset.sourceIndex);
    const source = indexedSources[index];

    if (!source || !source.source_id || !updateSourceFile.files.length) {
      updateSourceFile.value = "";
      return;
    }

    const formData = new FormData();
    formData.append("source_id", source.source_id);
    formData.append("notebook_id", notebookId);
    formData.append("file", updateSourceFile.files[0]);

    const res = await fetch("/update-source", {
      method: "POST",
      body: formData,
    });

    const data = await res.json();

    if (data.error) {
      alert(data.error);
      updateSourceFile.value = "";
      return;
    }

    indexedSources[index] = {
      source_id: data.source_id,
      source_name: data.source_name,
      source_type: data.source_type,
    };

    saveIndexedSources();
    renderSources();

    appendMessage(
      "assistant",
      `Updated source "${data.source_name}" and re-indexed ${data.chunks_indexed} chunks across ${data.pages_indexed} page(s).`
    );

    updateSourceFile.value = "";
  });
}

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();

  const question = questionInput.value.trim();

  if (!question || !notebookId) {
    alert("Ask an Admin to add a source first.");
    return;
  }

  appendMessage("user", question);
  questionInput.value = "";

  setChatFormDisabled(true);
  showChatLoading();

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        notebook_id: notebookId,
        question: question,
      }),
    });

    let data = {};

    try {
      data = await res.json();
    } catch {
      data = {
        error: `Chat request failed with status ${res.status}.`,
      };
    }
   
    if (!res.ok || data.error) {
      appendMessage(
        "assistant",
        data.error || "Unable to generate a chat response."
      );
      return;
    }

    appendMessage(
      "assistant",
      data.answer || "No response was returned.",
      data.citations || []
    );

  }
  catch (error) {
      console.error("Chat request failed:", error);

      appendMessage(
        "assistant",
        "Unable to generate a chat response. Please try again."
      );
    } finally {
      hideChatLoading();
      setChatFormDisabled(false);

      if (questionInput) {
        questionInput.focus();
      }
    }
});

if (summaryBtn) {
  summaryBtn.addEventListener("click", () => {
    if (!notebookId) {
      alert("Add a source first.");
      return;
    }

    openPromptModal("summary");
  });
}

if (quizBtn) {
  quizBtn.addEventListener("click", () => {
    if (!notebookId) {
      alert("Add a source first.");
      return;
    }

    openPromptModal("quiz");
  });
}

if (audioBtn) {
  audioBtn.addEventListener("click", () => {
    if (!notebookId) {
      alert("Add a source first.");
      return;
    }

    openPromptModal("audio");
  });
}

submitModalBtn.addEventListener("click", async () => {
  if (!notebookId || !activeAction) return;

  if (selectedRole !== "Admin") {
    alert("Only Admin can generate new outputs.");
    return;
  }

  const prompt = modalPrompt.value.trim();
  const action = activeAction;

  closePromptModal();

  if (action === "summary") {
    setActiveTab("summaryTab");

    const item = {
      title:
        document.getElementById("contentTitle").value.trim() ||
        "Untitled Summary",

    content: "Generating summary...",

    createdAt: new Date().toLocaleString(),
  };

    generatedSummaryItems.unshift(item);
    renderSummaryItems();
    saveGeneratedSummaryItems();

    const res = await fetch("/summary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        notebook_id: notebookId,
        prompt: prompt,
      }),
    });

    const data = await res.json();

    console.log("SUMMARY RESPONSE:", data);

    if (!res.ok || data.error) {
      item.content = data.error || "Summary generation failed.";
      renderSummaryItems();
      saveGeneratedSummaryItems();
      return;
    }

    const summaryText =
      data.summary ||
      data.content ||
      data.output?.content ||
      "";

    if (!summaryText.trim()) {
      item.content =
        "Summary generation returned an empty response. Please try again.";
    } else {
      item.content = summaryText;
    }

    item.content = data.summary || "";
    renderSummaryItems();
    saveGeneratedSummaryItems();
  }

  if (action === "quiz") {
    setActiveTab("quizTab");

    const item = {
        title:
            document.getElementById("contentTitle").value.trim() ||
            "Untitled Quiz",

        content: "Generating quiz...",

        createdAt: new Date().toLocaleString(),
    };

    generatedQuizItems.unshift(item);
    renderQuizItems();
    saveGeneratedQuizItems();

    const res = await fetch("/quiz", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        notebook_id: notebookId,
        prompt: prompt,
      }),
    });

    const data = await res.json();
    item.content = data.quiz || "";
    renderQuizItems();
    saveGeneratedQuizItems();
  }

  if (action === "audio") {
    setActiveTab("audioTab");

    const item = {
        id: crypto.randomUUID(),
        title:
            document.getElementById("contentTitle").value.trim() ||
            "Untitled Audio",

        script: "Generating audio overview...",

        audioUrl: "",

        createdAt: new Date().toLocaleString(),
    };

    generatedAudioItems.unshift(item);
    renderAudioItems();
    saveGeneratedAudioItems();

    const res = await fetch("/audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        notebook_id: notebookId,
        prompt: prompt,
      }),
    });

    const data = await res.json();

    item.script = data.script || "";

    item.audioUrl = data.audio_url
      ? `${data.audio_url}?v=${Date.now()}`
      : "";
    item.outputId = data.output_id || item.id;

    renderAudioItems();
    saveGeneratedAudioItems();

  }
});

const servicenowBtn = document.getElementById("servicenowBtn");
const salesforceBtn = document.getElementById("salesforceBtn");
const servicenowQuery = document.getElementById("servicenowQuery");
const servicenowLimit = document.getElementById("servicenowLimit");

if (servicenowBtn) {
  servicenowBtn.addEventListener("click", async () => {
    if (selectedRole !== "Admin") {
      alert("Only Admin can add ServiceNow sources.");
      return;
    }

    const query = servicenowQuery?.value.trim() || "";
    const limit = Number(servicenowLimit?.value || 100);

    showLoading(
      "Indexing ServiceNow",
      "Please wait while incidents, problems, and change requests are being loaded..."
    );

    try {
      const res = await fetch("/ingest-servicenow", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          notebook_id: notebookId || null,
          query: query,
          limit: limit,
        }),
      });

      const data = await res.json();

      console.log("ServiceNow response:", data);

      if (!res.ok) {
        throw new Error(
          data.error ||
            `ServiceNow indexing failed with HTTP ${res.status}.`
        );
      }

      if (data.error) {
        throw new Error(data.error);
      }

      if (data.notebook_id) {
        setNotebookId(data.notebook_id);
      }

      // The backend returns THREE separate sources.
      const results = Array.isArray(data.results)
        ? data.results
        : [];

      results.forEach((result) => {
        if (!result.source_id) {
          return;
        }

        appendSource({
          source_id: result.source_id,
          source_name: result.source_name,
          source_type: `servicenow-${result.table}`,
          file_count: result.records_indexed || 0,
        });
      });

      appendMessage(
        "assistant",
        `Indexed ${data.records_indexed || 0} ServiceNow records: ` +
          `${results.find(r => r.table === "incident")?.records_indexed || 0} incidents, ` +
          `${results.find(r => r.table === "problem")?.records_indexed || 0} problems, ` +
          `${results.find(r => r.table === "change_request")?.records_indexed || 0} change requests.`
      );

      showToast(
        `ServiceNow indexed successfully: ${data.records_indexed || 0} records.`
      );
    } catch (error) {
      console.error("ServiceNow indexing failed:", error);

      showError(
        error.message ||
          "ServiceNow indexing failed. Please try again."
      );
    } finally {
      hideLoading();
    }
  });
}

if (salesforceBtn) {
  salesforceBtn.addEventListener("click", async () => {
    if (selectedRole !== "Admin") {
      alert("Only Admin can add Salesforce sources.");
      return;
    }

    showLoading(
      "Indexing Salesforce",
      "Please wait while Accounts, Opportunities and Cases are being loaded..."
    );

    try {
      const res = await fetch("/ingest-salesforce", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          notebook_id: notebookId || null,
        }),
      });

      const data = await res.json();

      console.log("Salesforce response:", data);

      if (!res.ok) {
        throw new Error(
          data.error ||
            `Salesforce indexing failed with HTTP ${res.status}.`
        );
      }

      if (data.error) {
        throw new Error(data.error);
      }

      if (data.notebook_id) {
        setNotebookId(data.notebook_id);
      }

      appendSource({
          source_id: data.source_id,
          source_name: `Salesforce`,
          source_type: `salesforce`,
          file_count: data.records_indexed || 0,
      });

      showToast(
        `Salesforce indexed successfully: ${data.records_indexed || 0} records.`
      );
    } catch (error) {
      console.error("Salesforce indexing failed:", error);

      showError(
        error.message ||
          "Salesforce indexing failed. Please try again."
      );
    } finally {
      hideLoading();
    }
  });
}

const jiraBtn = document.getElementById("jiraBtn");
const jiraProjectKey = document.getElementById(
    "jiraProjectKey"
);

jiraBtn?.addEventListener(
    "click",
    async () => {

        const projectKey =
            jiraProjectKey.value
                .trim()
                .toUpperCase();

        if (!projectKey) {
            alert(
                "Please enter a JIRA project key."
            );
            return;
        }

        jiraBtn.disabled = true;
        jiraBtn.textContent =
            "Adding JIRA...";

        try {

            const response = await fetch(
                "/ingest-jira",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json",
                    },
                    body: JSON.stringify({
                        notebook_id:
                            notebookId,
                        project_key:
                            projectKey,
                    }),
                }
            );

            const data =
                await response.json();

            if (
                !response.ok ||
                data.error
            ) {
                throw new Error(
                    data.error ||
                    "Failed to add JIRA project."
                );
            }

            if (data.notebook_id) {
                setNotebookId(data.notebook_id);
            }

            appendSource(
                `JIRA - ${projectKey}`
            );

            jiraProjectKey.value = "";

            alert(
                `Successfully indexed ${data.records_indexed} JIRA tickets.`
            );

        } catch (error) {

            console.error(error);

            alert(error.message);

        } finally {

            jiraBtn.disabled = false;
            jiraBtn.textContent =
                "Add JIRA";

        }
    }
);


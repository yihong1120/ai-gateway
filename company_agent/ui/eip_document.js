const byId = (id) => document.getElementById(id);

function setOptional(row, target, value) {
  byId(row).hidden = !value;
  byId(target).textContent = value || "";
}

function showDocument(data) {
  byId("status").hidden = true;
  byId("document").hidden = false;
  byId("folder").textContent = data.folder_name;
  byId("subject").textContent = data.subject;
  byId("date").textContent = data.date;
  setOptional("due-row", "due", data.due_date);
  setOptional("flow-row", "flow", data.flow_number);
  setOptional("state-row", "state", data.status);
  const fields = byId("fields");
  for (const field of data.fields) {
    const row = document.createElement("div");
    const label = document.createElement("dt");
    const value = document.createElement("dd");
    label.textContent = field.label;
    value.textContent = field.value;
    row.append(label, value);
    fields.append(row);
  }
  const list = byId("attachments");
  if (!data.attachments.length) {
    const item = document.createElement("li");
    item.textContent = "這份公文沒有可檢視的 PDF 或 Word 附件。";
    list.append(item);
    return;
  }
  for (const attachment of data.attachments) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = attachment.url;
    link.textContent = attachment.name;
    link.target = "_blank";
    link.rel = "noopener";
    item.append(link);
    list.append(item);
  }
}

async function loadDocument() {
  const response = await fetch(`${location.pathname}/data`, {credentials: "same-origin"});
  if (!response.ok) throw response;
  showDocument(await response.json());
}

loadDocument();

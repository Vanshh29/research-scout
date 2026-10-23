// Posts the question and renders the answer. The answer and titles are model/ingested text, so they
// only ever reach the page through textContent and DOM nodes, never innerHTML.
(function () {
  var form = document.getElementById("ask-form");
  var status = document.getElementById("ask-status");
  var box = document.getElementById("ask-result");
  var answer = document.getElementById("ask-answer");
  var sources = document.getElementById("ask-sources");
  var heading = document.getElementById("ask-sources-h");

  function renderAnswer(text, cited) {
    answer.textContent = "";
    text.split(/(\[\d+\])/).forEach(function (part) {
      var m = /^\[(\d+)\]$/.exec(part);
      if (m && cited[m[1]]) {
        var a = document.createElement("a");
        a.href = "#src-" + m[1];
        a.textContent = part;
        answer.appendChild(a);
      } else {
        answer.appendChild(document.createTextNode(part));
      }
    });
  }

  function renderSources(list, label) {
    sources.textContent = "";
    heading.textContent = label;
    list.forEach(function (s) {
      var li = document.createElement("li");
      li.id = "src-" + s.n;
      var tag = document.createElement("span");
      tag.className = "muted";
      tag.textContent = "[" + s.n + "] ";
      li.appendChild(tag);
      if (s.url) {
        var a = document.createElement("a");
        a.href = s.url;
        a.rel = "noopener noreferrer";
        a.target = "_blank";
        a.textContent = s.title;
        li.appendChild(a);
      } else {
        li.appendChild(document.createTextNode(s.title));
      }
      sources.appendChild(li);
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var button = form.querySelector("button");
    button.disabled = true;
    status.textContent = "Thinking.";
    fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scout": "1" },
      body: JSON.stringify({ question: form.elements.question.value }),
    })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || "HTTP " + r.status);
          return body;
        });
      })
      .then(function (data) {
        var cited = {};
        data.sources.forEach(function (s) { cited[s.n] = true; });
        renderAnswer(data.answer, cited);
        if (data.grounded) {
          renderSources(data.sources, "Sources");
        } else {
          renderSources(data.retrieved, "Items searched (none were cited)");
        }
        box.hidden = false;
        status.textContent = data.cached ? "Answered from cache." : "";
      })
      .catch(function (err) {
        box.hidden = true;
        status.textContent = "Could not answer: " + err.message;
      })
      .finally(function () { button.disabled = false; });
  });
})();

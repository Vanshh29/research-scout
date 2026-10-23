// Thumbs up/down: POST the vote with the X-Scout header (CSRF guard), then mirror the server's answer.
(function () {
  var status = document.getElementById("vote-status");

  function show(message) {
    status.textContent = message;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("button.vote");
    if (!button) return;
    var item = button.getAttribute("data-item");
    var vote = Number(button.getAttribute("data-vote"));
    fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scout": "1" },
      body: JSON.stringify({ item_id: item, vote: vote }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status === 401 ? "locked, unlock again" : "HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        document.querySelectorAll("button.vote").forEach(function (b) {
          if (b.getAttribute("data-item") === item) {
            b.setAttribute("aria-pressed", String(Number(b.getAttribute("data-vote")) === data.vote));
          }
        });
        show("");
      })
      .catch(function (err) {
        show("Could not save the vote: " + err.message);
      });
  });
})();

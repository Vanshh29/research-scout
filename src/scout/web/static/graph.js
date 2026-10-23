// Draws /api/graph with Cytoscape. Every label reaches the page through textContent or a
// Cytoscape data field (never innerHTML): entity names and titles are untrusted model output.
(function () {
  var canvas = document.getElementById("graph");
  var status = document.getElementById("graph-status");
  var list = document.getElementById("graph-items");
  var cy = null;

  function setItems(items, name) {
    list.textContent = "";
    if (!items.length) {
      var none = document.createElement("li");
      none.className = "muted";
      none.textContent = name ? "No items mention " + name + "." : "Select a concept to see the items that mention it.";
      list.appendChild(none);
      return;
    }
    items.forEach(function (item) {
      var li = document.createElement("li");
      if (item.url) {
        var a = document.createElement("a");
        a.href = item.url;
        a.rel = "noopener noreferrer";
        a.target = "_blank";
        a.textContent = item.title;
        li.appendChild(a);
      } else {
        li.textContent = item.title;
      }
      list.appendChild(li);
    });
  }

  function draw(data) {
    if (cy) cy.destroy();
    cy = cytoscape({
      container: canvas,
      elements: data.nodes.map(function (n) {
        return { data: { id: "n" + n.id, label: n.name, type: n.type, degree: n.degree, center: n.name === data.center } };
      }).concat(data.edges.map(function (e) {
        return { data: { source: "n" + e.src, target: "n" + e.dst, label: e.label } };
      })),
      layout: { name: "cose", animate: false, nodeRepulsion: 9000 },
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, "text-wrap": "ellipsis", "text-max-width": 90, "background-color": "#6b7a90", width: "mapData(degree, 1, 20, 14, 38)", height: "mapData(degree, 1, 20, 14, 38)" } },
        { selector: "node[type = 'Paper'], node[type = 'Post']", style: { shape: "round-rectangle", "background-color": "#b08d57" } },
        { selector: "node[type = 'Person']", style: { shape: "diamond", "background-color": "#7a9b76" } },
        { selector: "node[?center]", style: { "background-color": "#3b6fd4", "border-width": 3, "border-color": "#1c3d8c" } },
        { selector: "edge", style: { width: 1, "line-color": "#aab2bf", "curve-style": "haystack" } },
        { selector: "edge[label != 'mentions'][label != 'authored_by']", style: { width: 2, "line-color": "#6b7a90", label: "data(label)", "font-size": 8, "curve-style": "bezier" } },
      ],
    });
    cy.on("tap", "node", function (evt) {
      var node = evt.target;
      if (node.data("type") === "Paper" || node.data("type") === "Post" || node.data("type") === "Person") return;
      load(node.data("label"));
    });
  }

  function load(center) {
    var url = "/api/graph" + (center ? "?center=" + encodeURIComponent(center) + "&depth=2" : "");
    status.textContent = "Loading the graph.";
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        draw(data);
        setItems(data.items, data.center);
        if (!data.nodes.length) {
          status.textContent = center ? "No concept matches " + center + "." : "The graph is empty. Run scout build-graph first.";
        } else {
          status.textContent = (data.center ? data.center + ": " : "Overview: ") + data.nodes.length + " nodes, " + data.edges.length + " edges" + (data.truncated ? " (truncated)" : "") + ". Click a concept to recentre.";
        }
      })
      .catch(function (err) { status.textContent = "Could not load the graph: " + err.message; });
  }

  load(canvas.getAttribute("data-center"));
})();

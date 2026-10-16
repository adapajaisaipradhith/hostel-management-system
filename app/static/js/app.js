document.addEventListener("click", (event) => {
  const confirmButton = event.target.closest("[data-confirm]");
  if (confirmButton && !window.confirm(confirmButton.dataset.confirm)) {
    event.preventDefault();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  const canvas = document.querySelector("#occupancy-chart");
  if (!canvas || typeof Chart === "undefined") return;

  const data = JSON.parse(canvas.dataset.chart);
  new Chart(canvas, {
    type: "bar",
    data: {
      labels: data.labels,
      datasets: [
        { label: "Physically occupied", data: data.occupied, backgroundColor: "#21654f" },
        { label: "Total active beds", data: data.beds, backgroundColor: "#d9e86d" },
      ],
    },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } },
  });
});
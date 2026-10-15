"use strict";
document.querySelectorAll("[data-class-filter]").forEach(input => {
  input.addEventListener("input", () => {
    const query = input.value.toLocaleLowerCase("ru");
    const select = document.getElementById(input.dataset.classFilter);
    for (const option of select.options) {
      option.hidden = !option.selected && !option.textContent.toLocaleLowerCase("ru").includes(query);
    }
  });
});

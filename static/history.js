"use strict";

const deleteForm = document.getElementById("delete-form");

if (deleteForm) {
  const dateInput = deleteForm.querySelector('input[name="date"]');
  const confirmInput = document.getElementById("confirm_date");

  deleteForm.addEventListener("submit", (event) => {
    const businessDate = dateInput.value;
    const firstConfirmation = window.confirm(
      `确定删除 ${businessDate} 的全部会话和字幕吗？此操作无法撤销。`,
    );
    if (!firstConfirmation) {
      event.preventDefault();
      return;
    }
    if (confirmInput.value.trim() !== businessDate) {
      window.alert(`请准确输入 ${businessDate} 完成第二次确认。`);
      event.preventDefault();
      confirmInput.focus();
    }
  });
}

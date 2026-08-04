document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".video-wrapper.video-lazy").forEach((wrapper) => {
    const button = wrapper.querySelector(".video-play-button");
    const thumb = wrapper.querySelector(".video-thumb");

    if (!button || !thumb) return;

    const playVideo = () => {
      const youtubeId = wrapper.dataset.youtubeId;
      if (!youtubeId || !/^[A-Za-z0-9_-]{11}$/.test(youtubeId)) return;
      if (wrapper.classList.contains("is-playing")) return;

      wrapper.classList.add("is-playing");
      const iframe = document.createElement("iframe");
      iframe.className = "video-iframe";
      iframe.src = `https://www.youtube-nocookie.com/embed/${youtubeId}?autoplay=1&rel=0`;
      iframe.title = thumb.alt || "YouTube video player";
      iframe.allow =
        "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
      iframe.allowFullscreen = true;
      iframe.loading = "lazy";

      iframe.addEventListener("load", () => {
        iframe.classList.add("is-ready");
        thumb.remove();
        button.remove();
      });

      wrapper.appendChild(iframe);
    };

    button.addEventListener("click", playVideo);
    thumb.addEventListener("click", playVideo);
  });

  document.querySelectorAll(".delete-article-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const title = form.dataset.confirmTitle || "this article";
      if (!window.confirm(`Delete “${title}”? This cannot be undone.`)) {
        event.preventDefault();
      }
    });
  });
});

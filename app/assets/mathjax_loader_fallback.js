// fallback MathJax CDN logic in case of offline mode
let script = document.createElement("script");
script.src = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js";  // Main CDN

// If CDN fails, load from local fallback
script.onerror = () => {
    console.warn("CDN failed. Loading fallback MathJax...");
    let fallbackScript = document.createElement("script");
    fallbackScript.src = "/assets/fallback-mathjax/tex-chtml.js";  // Make sure this exists!
    document.head.appendChild(fallbackScript);
};

document.head.appendChild(script);

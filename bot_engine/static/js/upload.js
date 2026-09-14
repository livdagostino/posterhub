var MAX_SIZE_MB   = 20;
var ALLOWED_TYPES = ['image/jpeg','image/png','image/gif','image/webp','image/bmp','image/tiff','image/heic','image/heif'];
var ALLOWED_EXTS  = ['jpg','jpeg','png','gif','webp','bmp','tiff','tif','heic','heif'];

var dropZone    = document.getElementById('dropZone');
var fileInput   = document.getElementById('hiddenFileInput');
var dropIdle    = document.getElementById('dropIdle');
var dropPreview = document.getElementById('dropPreview');
var previewImg  = document.getElementById('previewImg');
var removeBtn   = document.getElementById('removeFile');
var metaBox     = document.getElementById('fileMetaInfo');
var fileNameVal = document.getElementById('fileNameValue');
var fileSizeVal = document.getElementById('fileSizeValue');
var fileDimVal  = document.getElementById('fileDimensionsValue');

var multiPreview = document.getElementById('multiPreview');
var multiSummary = document.getElementById('multiSummary');
var multiGrid    = document.getElementById('multiGrid');
var selectedFiles = [];


var USER_GROUPS = (function () {
    var out = [];
    document.querySelectorAll('#uploadGroupChips input[name="group_ids"]').forEach(function (cb) {
        out.push({
            id: cb.value,
            name: (cb.parentNode.textContent || '').replace('★', '').trim(),
            isPrimary: cb.defaultChecked,
        });
    });
    return out;
})();

function defaultGroupSelection() {
    var primary = USER_GROUPS.filter(function (g) { return g.isPrimary; });
    var first = primary.length ? [primary[0].id] : (USER_GROUPS.length ? [USER_GROUPS[0].id] : []);
    return new Set(first);
}


var groupsByFile = new Map();

function ensureFileGroups(file) {
    if (!groupsByFile.has(file)) {
        groupsByFile.set(file, defaultGroupSelection());
    }
    return groupsByFile.get(file);
}

function getFileExtension(name) {
    var parts = name.split('.');
    return parts.length > 1 ? parts.pop().toLowerCase() : '';
}

function isHeicFile(file) {
    var ext = getFileExtension(file.name);
    return ext === 'heic' || ext === 'heif' || file.type === 'image/heic' || file.type === 'image/heif';
}

function formatBytes(bytes) {
    if (!bytes) return '-';
    var kb = bytes / 1024, mb = kb / 1024;
    return mb >= 1 ? mb.toFixed(2) + ' MB' : kb.toFixed(1) + ' KB';
}

function showError(msg) {
    var box = document.getElementById('uploadError');
    document.getElementById('uploadErrorMsg').textContent = msg;
    box.style.display = 'flex';
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function dismissError() {
    document.getElementById('uploadError').style.display = 'none';
}

function resetDropZone() {
    selectedFiles = [];
    groupsByFile.clear();
    dropIdle.style.display    = '';
    dropIdle.classList.remove('drop-zone-idle-compact');
    dropPreview.style.display = 'none';
    previewImg.src            = '';
    previewImg.style.display  = '';
    var oldPh = dropPreview.querySelector('.heic-placeholder');
    if (oldPh) oldPh.remove();
    metaBox.style.display     = 'none';
    multiPreview.style.display = 'none';
    multiGrid.innerHTML = '';
    fileInput.value           = '';
    document.getElementById('uploadExtras').style.display = 'none';
    document.getElementById('submitBtn').textContent = 'Analyze Poster';
}

function syncFileInput() {
    var dt = new DataTransfer();
    selectedFiles.forEach(function (f) { dt.items.add(f); });
    fileInput.files = dt.files;
}

function validateFile(file) {
    var typeOk = ALLOWED_TYPES.indexOf(file.type) !== -1;
    if (!typeOk && file.type === '') {
        typeOk = ALLOWED_EXTS.indexOf(getFileExtension(file.name)) !== -1;
    }
    if (!typeOk) {
        showError('"' + file.name + '" is not a valid image. Allowed: JPG, PNG, GIF, WEBP, BMP, TIFF, HEIC.');
        return false;
    }
    if (file.size / (1024 * 1024) > MAX_SIZE_MB) {
        showError('"' + file.name + '" is too large (' + (file.size / (1024*1024)).toFixed(1) + ' MB). Max: ' + MAX_SIZE_MB + ' MB.');
        return false;
    }
    return true;
}

function removeFileAt(index) {
    var removed = selectedFiles.splice(index, 1)[0];
    if (removed) groupsByFile.delete(removed);
    syncFileInput();
    if (selectedFiles.length === 0) {
        resetDropZone();
    } else {
        renderPreviews();
    }
}

function renderPreviews() {
    dismissError();

    var extrasPanel = document.getElementById('uploadExtras');
    var notesField  = document.getElementById('extrasNotesField');
    var tagsField   = document.getElementById('extrasTagsField');
    var groupsField = document.getElementById('extrasGroupsField');
    var hintSingle  = document.getElementById('groupsHintSingle');
    var hintMulti   = document.getElementById('groupsHintMulti');

    if (selectedFiles.length === 1) {

        extrasPanel.style.display = 'block';
        if (groupsField) groupsField.style.display = '';
        if (notesField)  notesField.style.display  = '';
        if (tagsField)   tagsField.style.display   = '';
        if (hintSingle)  hintSingle.style.display  = '';
        if (hintMulti)   hintMulti.style.display   = 'none';
        multiPreview.style.display = 'none';
        var file = selectedFiles[0];
        var heic = isHeicFile(file);


        var oldPh = dropPreview.querySelector('.heic-placeholder');
        if (oldPh) oldPh.remove();

        if (heic) {
            previewImg.src = '';
            previewImg.alt = file.name;
            previewImg.style.display = 'none';
            var placeholder = document.createElement('div');
            placeholder.className = 'heic-placeholder';
            placeholder.innerHTML = '<span class="heic-placeholder-icon">🖼</span><span class="heic-placeholder-name"></span><span>HEIC preview not available in browser</span>';
            placeholder.querySelector('.heic-placeholder-name').textContent = file.name;
            dropPreview.insertBefore(placeholder, dropPreview.firstChild);
        } else {
            previewImg.style.display = '';
            var url = URL.createObjectURL(file);
            previewImg.onload = function () { URL.revokeObjectURL(url); };
            previewImg.src = url;
        }

        dropIdle.style.display    = 'none';
        dropIdle.classList.remove('drop-zone-idle-compact');
        dropPreview.style.display = 'flex';
        fileNameVal.textContent = file.name;
        fileSizeVal.textContent = formatBytes(file.size);
        metaBox.style.display   = 'flex';

        if (heic) {
            fileDimVal.textContent = 'N/A (HEIC)';
        } else {
            fileDimVal.textContent = 'Loading…';
            var tempImg = new Image();
            var tempUrl = URL.createObjectURL(file);
            tempImg.onload = function () {
                fileDimVal.textContent = tempImg.naturalWidth + ' × ' + tempImg.naturalHeight + ' px';
                URL.revokeObjectURL(tempUrl);
            };
            tempImg.src = tempUrl;
        }
    } else {
        extrasPanel.style.display = 'none';
        dropPreview.style.display = 'none';
        metaBox.style.display     = 'none';
        dropIdle.style.display    = '';
        dropIdle.classList.add('drop-zone-idle-compact');

        var totalSize = 0;
        selectedFiles.forEach(function (f) { totalSize += f.size; });
        multiSummary.innerHTML = '<strong>' + selectedFiles.length + ' images selected</strong> · ' + formatBytes(totalSize)
            + '<div style="font-size:0.88em;color:var(--text-muted);margin-top:4px;font-weight:400;">'
            + 'Pick one or more groups for each paper. The primary is preselected.</div>';
        multiGrid.innerHTML = '';

        selectedFiles.forEach(function (file, idx) {
            ensureFileGroups(file);

            var row = document.createElement('div');
            row.className = 'multi-row';

            var thumb;
            if (isHeicFile(file)) {
                thumb = document.createElement('div');
                thumb.className = 'multi-row-thumb multi-thumb-heic';
                thumb.textContent = '🖼';
            } else {
                thumb = document.createElement('img');
                thumb.className = 'multi-row-thumb';
                var thumbUrl = URL.createObjectURL(file);
                thumb.onload = function () { URL.revokeObjectURL(thumbUrl); };
                thumb.src = thumbUrl;
            }
            row.appendChild(thumb);

            var info = document.createElement('div');
            info.className = 'multi-row-info';

            var nameEl = document.createElement('div');
            nameEl.className = 'multi-row-name';
            nameEl.textContent = file.name;
            nameEl.title = file.name;
            info.appendChild(nameEl);

            var metaEl = document.createElement('div');
            metaEl.className = 'multi-row-meta';
            metaEl.textContent = formatBytes(file.size);
            info.appendChild(metaEl);

            if (USER_GROUPS.length) {
                var chips = document.createElement('div');
                chips.className = 'multi-row-groups';
                USER_GROUPS.forEach(function (g) {
                    var label = document.createElement('label');
                    label.className = 'group-chip';
                    label.style.cssText = 'display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border:1.5px solid var(--border);border-radius:20px;background:var(--bg-input);cursor:pointer;font-size:0.74em;transition:all 0.18s;';
                    var cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.value = g.id;
                    cb.style.accentColor = 'var(--primary)';
                    cb.checked = groupsByFile.get(file).has(g.id);
                    cb.addEventListener('change', function () {
                        var sel = groupsByFile.get(file);
                        if (cb.checked) sel.add(g.id);
                        else             sel.delete(g.id);
                    });
                    label.appendChild(cb);
                    label.appendChild(document.createTextNode(' ' + g.name + (g.isPrimary ? ' ★' : '')));
                    chips.appendChild(label);
                });
                info.appendChild(chips);
            }
            row.appendChild(info);

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'multi-row-remove';
            btn.textContent = '✕';
            btn.title = 'Remove ' + file.name;
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                removeFileAt(idx);
            });
            row.appendChild(btn);

            multiGrid.appendChild(row);
        });

        multiPreview.style.display = 'block';
    }

    var submitBtn = document.getElementById('submitBtn');
    submitBtn.textContent = selectedFiles.length > 1
        ? 'Analyze ' + selectedFiles.length + ' Posters'
        : 'Analyze Poster';
}

function handleFiles(fileList) {
    dismissError();
    var newFiles = [];
    for (var i = 0; i < fileList.length; i++) {
        if (validateFile(fileList[i])) {
            newFiles.push(fileList[i]);
        }
    }
    if (newFiles.length === 0) return;
    selectedFiles = newFiles;
    syncFileInput();
    renderPreviews();
}

dropZone.addEventListener('click', function (e) {
    if (e.target === removeBtn || removeBtn.contains(e.target)) return;
    if (multiGrid.contains(e.target)) return;
    fileInput.click();
});
fileInput.addEventListener('change', function () {
    if (fileInput.files && fileInput.files.length) handleFiles(fileInput.files);
});
removeBtn.addEventListener('click', function (e) {
    e.stopPropagation();
    resetDropZone();
    dismissError();
});
dropZone.addEventListener('dragover', function (e) {
    e.preventDefault();
    dropZone.classList.add('drag-over');
});
['dragleave', 'dragend'].forEach(function (ev) {
    dropZone.addEventListener(ev, function () { dropZone.classList.remove('drag-over'); });
});
dropZone.addEventListener('drop', function (e) {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
        handleFiles(e.dataTransfer.files);
    }
});

var progressWrap   = document.getElementById('uploadProgressWrap');
var progressFill   = document.getElementById('progressFill');
var progressPct    = document.getElementById('progressPct');
var progressPhase  = document.getElementById('progressPhase');
var progressStatus = document.getElementById('progressStatus');

function setProgress(pct, phase, status) {
    progressFill.style.width = pct + '%';
    progressPct.textContent  = Math.round(pct) + '%';
    if (phase)  progressPhase.textContent  = phase;
    if (status) progressStatus.textContent = status;
}

function showProgress() {
    progressWrap.style.display = 'block';
    var label = selectedFiles.length > 1
        ? 'Uploading ' + selectedFiles.length + ' images…'
        : 'Uploading…';
    setProgress(0, label, '');
}

document.getElementById('uploadForm').addEventListener('submit', function (e) {
    if (!selectedFiles.length) {
        e.preventDefault();
        showError('Please select or drop at least one image before submitting.');
        return;
    }
    e.preventDefault();
    dismissError();

    var submitBtn = document.getElementById('submitBtn');
    submitBtn.disabled = true;
    showProgress();

    var formData = new FormData(document.getElementById('uploadForm'));
    formData.delete('image');
    selectedFiles.forEach(function (f) { formData.append('image', f); });

    if (selectedFiles.length > 1) {
        formData.delete('group_ids');
        selectedFiles.forEach(function (f, i) {
            var sel = groupsByFile.get(f);
            if (!sel) return;
            sel.forEach(function (gid) {
                formData.append('group_ids_' + i, gid);
            });
        });
    }

    var xhr = new XMLHttpRequest();

    xhr.upload.addEventListener('progress', function (ev) {
        if (ev.lengthComputable) {
            setProgress(
                (ev.loaded / ev.total) * 100,
                selectedFiles.length > 1
                    ? 'Uploading ' + selectedFiles.length + ' images…'
                    : 'Uploading…',
                formatBytes(ev.loaded) + ' / ' + formatBytes(ev.total)
            );
        }
    });

    xhr.upload.addEventListener('load', function () {
        setProgress(100, 'Upload complete', 'Redirecting…');
    });

    xhr.addEventListener('load', function () {
        var contentType = xhr.getResponseHeader('Content-Type') || '';
        if (contentType.indexOf('application/json') !== -1) {
            try {
                var data = JSON.parse(xhr.responseText);
                if (data.redirect) {
                    window.location.href = data.redirect;
                    return;
                }
                if (data.error) {
                    submitBtn.disabled = false;
                    progressWrap.style.display = 'none';
                    showError(data.message || 'Upload failed. Please try again.');
                    return;
                }
            } catch (e) {  }
        }
        if (xhr.status >= 200 && xhr.status < 400) {
            window.location.href = xhr.responseURL || '/dashboard/';
        } else {
            submitBtn.disabled = false;
            progressWrap.style.display = 'none';
            showError('Server error ' + xhr.status + '. Please try again.');
        }
    });

    xhr.addEventListener('error', function () {
        submitBtn.disabled = false;
        progressWrap.style.display = 'none';
        showError('Network error. Please check your connection and try again.');
    });

    xhr.open('POST', document.getElementById('uploadForm').action || window.location.href);
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.send(formData);
});

(function () {
    function bindCounter(inputId, counterId, max) {
        var el = document.getElementById(inputId);
        var counter = document.getElementById(counterId);
        if (!el || !counter) return;
        counter.textContent = el.value.length;
        el.addEventListener('input', function () {
            counter.textContent = el.value.length;
            counter.parentElement.classList.toggle('over-limit', el.value.length >= max);
        });
    }
    bindCounter('id_notes', 'notesCharCount', 500);
    bindCounter('id_tags', 'tagsCharCount', 200);
})();

(function () {
    function bindToggle(buttonId, bodyId) {
        var btn = document.getElementById(buttonId);
        var body = document.getElementById(bodyId);
        if (!btn || !body) return;
        btn.addEventListener('click', function () {
            var open = btn.classList.toggle('open');
            body.style.display = open ? 'block' : 'none';
        });
    }
    document.addEventListener('DOMContentLoaded', function () {
        bindToggle('tipsToggle', 'tipsBody');
        bindToggle('recentToggle', 'recentList');
    });
})();

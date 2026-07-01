(function(){
  'use strict';
  // --- helpers ---
  // Auth helpers (_tok, _auth) provided by shared auth.js
  function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

  function _fmtSize(bytes){
    if(bytes==null||bytes===undefined) return '';
    if(bytes<1024) return bytes+' B';
    if(bytes<1048576) return (bytes/1024).toFixed(1)+' KB';
    if(bytes<1073741824) return (bytes/1048576).toFixed(1)+' MB';
    return (bytes/1073741824).toFixed(1)+' GB';
  }

  // File extension -> category mapping
  var _EXT_MAP={
    // documents
    pdf:'documents',doc:'documents',docx:'documents',odt:'documents',rtf:'documents',
    xls:'documents',xlsx:'documents',ppt:'documents',pptx:'documents',
    // markdown
    md:'markdown',mdx:'markdown',
    // code
    py:'code',js:'code',ts:'code',jsx:'code',tsx:'code',html:'code',htm:'code',
    css:'code',sh:'code',bash:'code',yml:'code',yaml:'code',toml:'code',
    rs:'code',go:'code',java:'code',c:'code',cpp:'code',h:'code',rb:'code',
    // images
    png:'images',jpg:'images',jpeg:'images',gif:'images',webp:'images',svg:'images',bmp:'images',ico:'images',
    // audio
    mp3:'audio',wav:'audio',ogg:'audio',flac:'audio',aac:'audio',m4a:'audio',wma:'audio',
    // video
    mp4:'video',avi:'video',mkv:'video',mov:'video',webm:'video',wmv:'video',flv:'video',
    // data
    csv:'data',json:'data',xml:'data',sql:'data',db:'data',sqlite:'data',tsv:'data'
  };

  // File extension -> icon class mapping (uses existing Vortex CSS classes)
  var _ICON_MAP={
    pdf:'fc-pdf',doc:'fc-doc',docx:'fc-doc',odt:'fc-doc',rtf:'fc-doc',
    xls:'fc-xlsx',xlsx:'fc-xlsx',ppt:'fc-xlsx',pptx:'fc-xlsx',
    md:'fc-md',mdx:'fc-md',
    py:'fc-code',js:'fc-code',ts:'fc-code',jsx:'fc-code',tsx:'fc-code',
    sh:'fc-code',bash:'fc-code',yml:'fc-code',yaml:'fc-code',toml:'fc-code',
    rs:'fc-code',go:'fc-code',java:'fc-code',c:'fc-code',cpp:'fc-code',h:'fc-code',rb:'fc-code',
    html:'fc-html',htm:'fc-html',css:'fc-code',
    png:'fc-img',jpg:'fc-img',jpeg:'fc-img',gif:'fc-img',webp:'fc-img',svg:'fc-img',bmp:'fc-img',ico:'fc-img',
    csv:'fc-csv',tsv:'fc-csv',
    json:'fc-json',xml:'fc-json',sql:'fc-csv',db:'fc-csv',sqlite:'fc-csv'
  };

  // GDrive mimeType -> category mapping
  var _MIME_CAT_MAP={
    'application/pdf':'documents',
    'application/vnd.google-apps.document':'documents',
    'application/msword':'documents',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'documents',
    'application/vnd.google-apps.spreadsheet':'data',
    'application/vnd.ms-excel':'data',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'data',
    'application/vnd.google-apps.presentation':'documents',
    'application/vnd.ms-powerpoint':'documents',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation':'documents',
    'text/markdown':'markdown',
    'text/x-python':'code',
    'application/javascript':'code',
    'text/javascript':'code',
    'text/html':'code',
    'text/css':'code',
    'application/json':'data',
    'text/csv':'data',
    'text/xml':'data',
    'application/xml':'data',
    'application/vnd.google-apps.folder':'folders'
  };

  function _getMimeCat(mimeType){
    if(!mimeType) return 'other';
    if(_MIME_CAT_MAP[mimeType]) return _MIME_CAT_MAP[mimeType];
    if(mimeType.indexOf('image/')===0) return 'images';
    if(mimeType.indexOf('audio/')===0) return 'audio';
    if(mimeType.indexOf('video/')===0) return 'video';
    if(mimeType.indexOf('text/')===0) return 'code';
    return 'other';
  }

  var _FOLDER_SVG='<svg viewBox="0 0 24 24" style="fill:none;stroke:var(--pb-yellow);stroke-width:2;width:32px;height:32px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
  var _FILE_SVG='<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

  function _getExt(name){
    var dot=name.lastIndexOf('.');
    return dot>0?name.substring(dot+1).toLowerCase():'';
  }
  function _getCat(name){ return _EXT_MAP[_getExt(name)]||'other'; }
  function _getIconClass(name){ return _ICON_MAP[_getExt(name)]||'fc-code'; }
  function _getTypeLabel(name){
    var ext=_getExt(name);
    return ext?ext.toUpperCase():'FILE';
  }

  // --- state ---
  var _currentDir=null;   // null = root (show allowed dirs)
  var _allItems=[];       // flat list of items in current dir
  var _currentCat='all';  // active filter category
  var _viewMode='grid';   // 'grid' or 'list'
  var _searchQuery='';
  var _rootDirs=[]; // allowed base directories (stored on first root load)
  var _source='local'; // 'local' or 'gdrive'
  var _selectedItem=null; // currently selected item for detail panel

  // --- DOM refs ---
  var _gridContent=null, _listContent=null;
  var _bcEl=null, _bcCur=null;
  var _infoCount=null, _infoSort=null;

  function _initRefs(){
    _gridContent=document.getElementById('files-grid-content');
    _listContent=document.getElementById('files-list-content');
    _bcEl=document.getElementById('files-breadcrumb');
    _bcCur=document.getElementById('files-bc-cur');
    _infoCount=document.getElementById('files-info-count');
    _infoSort=document.getElementById('files-info-sort');
  }

  // --- loading spinner HTML ---
  var _LOADING_HTML='<div style="padding:48px;text-align:center;color:var(--pb-text-dim);font-size:13px"><div class="gdrive-spinner" style="width:24px;height:24px;margin:0 auto 12px"></div>Loading files...</div>';

  // --- API calls ---
  function _loadFiles(dir){
    if(!_tok()) return;
    _currentDir=dir;
    _currentCat='all';
    _source='local';
    _hideDetailPanel();
    // Show loading spinner
    if(_gridContent) _gridContent.innerHTML=_LOADING_HTML;
    if(_listContent) _listContent.innerHTML='';

    var url='/api/download/list';
    if(dir) url+='?dir='+encodeURIComponent(dir);
    fetch(url,{headers:_auth()})
      .then(function(r){
        if(!r.ok) throw new Error('HTTP '+r.status);
        return r.json();
      })
      .then(function(d){
        _allItems=[];
        if(d.dirs){
          // Root view: show allowed base directories
          _rootDirs=d.dirs;
          d.dirs.forEach(function(dd){
            var name=dd.split('/').pop()||dd;
            _allItems.push({name:name,path:dd,is_dir:true,size:null,_rootDir:true,source:'local'});
          });
        } else if(d.items){
          _allItems=d.items.map(function(it){ it.source='local'; return it; });
        }
        _updateBreadcrumb(dir);
        _renderView();
        _updateSidebarCounts();
        _updateStorageBar();
        if(!dir) _updateQuickNav();
        // Activate Overview in sidebar
        var ftItems=document.querySelectorAll('#ftSecFilters .ft-item');
        ftItems.forEach(function(c){c.classList.remove('active');});
        if(ftItems[0]) ftItems[0].classList.add('active');
      })
      .catch(function(err){
        if(_gridContent) _gridContent.innerHTML='<div style="padding:24px;text-align:center;color:var(--pb-error);font-size:13px;">Error loading files: '+_esc(String(err))+'</div>';
      });
  }

  // --- setItems: external source provides normalized items ---
  function _setItems(items, source){
    if(!_gridContent||!_listContent) _initRefs();
    _source=source||'local';
    _allItems=items;
    _currentCat='all';
    _hideDetailPanel();
    _renderView();
    _updateSidebarCounts();
  }

  // --- breadcrumb ---
  function _updateBreadcrumb(dir){
    if(!_bcEl) return;
    _bcEl.innerHTML='';
    var rootSpan=document.createElement('span');
    rootSpan.textContent='Root';
    rootSpan.style.cursor='pointer';
    rootSpan.style.color='var(--pb-blue)';
    rootSpan.onclick=function(){ _loadFiles(null); };
    _bcEl.appendChild(rootSpan);
    if(!dir) return;

    // Find which root dir this path belongs to, only show segments from there
    var matchedRoot='';
    _rootDirs.forEach(function(rd){ if(dir===rd || dir.indexOf(rd+'/')===0) matchedRoot=rd; });
    var parts=dir.split('/').filter(Boolean);
    // Find the index where the root dir ends
    var rootParts=matchedRoot.split('/').filter(Boolean);
    var startIdx=rootParts.length-1; // start from the root dir name

    for(var i=startIdx;i<parts.length;i++){
      (function(idx){
        var sep=document.createElement('span');
        sep.className='bc-sep';
        sep.textContent=' / ';
        _bcEl.appendChild(sep);
        var partial='/'+parts.slice(0,idx+1).join('/');
        if(idx<parts.length-1){
          var link=document.createElement('span');
          link.textContent=parts[idx];
          link.style.cursor='pointer';
          link.style.color='var(--pb-blue)';
          link.onclick=function(){ _loadFiles(partial); };
          _bcEl.appendChild(link);
        } else {
          var cur=document.createElement('span');
          cur.className='bc-cur';
          cur.textContent=parts[idx];
          _bcEl.appendChild(cur);
        }
      })(i);
    }
  }

  // --- set breadcrumb for GDrive path ---
  function _setGdriveBreadcrumb(pathArray){
    // pathArray: [{id:'root',name:'My Drive'}, {id:'abc',name:'Folder'}]
    if(!_bcEl) return;
    _bcEl.innerHTML='';
    pathArray.forEach(function(p,i){
      if(i>0){
        var sep=document.createElement('span');
        sep.className='bc-sep';
        sep.textContent=' / ';
        _bcEl.appendChild(sep);
      }
      var s=document.createElement('span');
      s.textContent=p.name;
      if(i===pathArray.length-1){
        s.className='bc-cur';
      } else {
        s.style.cursor='pointer';
        s.style.color='var(--pb-blue)';
        (function(entry,idx){
          s.onclick=function(){
            if(typeof window.gdriveBrowse==='function') window.gdriveBrowse(entry.id);
          };
        })(p,i);
      }
      _bcEl.appendChild(s);
    });
  }

  // --- filter by category ---
  function _filterCategory(cat){
    _currentCat=cat;
    // Update sidebar active state
    var ftItems=document.querySelectorAll('#ftSecFilters .ft-item');
    ftItems.forEach(function(c){c.classList.remove('active');});
    ftItems.forEach(function(c){
      var oc=c.getAttribute('onclick')||'';
      if(oc.indexOf("'"+cat+"'")>-1) c.classList.add('active');
    });
    _renderView();
  }

  // --- filter items by current cat + search ---
  function _getFilteredItems(){
    var items=_allItems;
    if(_currentCat==='folders'){
      items=items.filter(function(it){ return !!it.is_dir; });
    } else if(_currentCat!=='all'){
      items=items.filter(function(it){
        if(it.is_dir) return false; // dirs only show in 'all' or 'folders'
        var cat=(it.source==='gdrive')?_getMimeCat(it.mimeType):_getCat(it.name);
        return cat===_currentCat;
      });
    }
    if(_searchQuery){
      var q=_searchQuery.toLowerCase();
      items=items.filter(function(it){
        return it.name.toLowerCase().indexOf(q)>-1;
      });
    }
    return items;
  }

  // =========================================================================
  // FEATURE 2: File Detail/Preview Panel
  // =========================================================================

  function _getDetailPanelEl(){
    var panel=document.getElementById('file-detail-panel');
    if(!panel){
      panel=document.createElement('div');
      panel.id='file-detail-panel';
      panel.className='file-detail-panel';
      panel.style.cssText='display:none;position:absolute;top:0;right:0;bottom:0;width:320px;background:var(--pb-surface-alt,rgba(15,20,35,0.98));border-left:1px solid var(--pb-border);padding:20px;overflow-y:auto;z-index:50;transition:transform .2s ease;';
      // Insert into files content area
      var filesContent=document.getElementById('files-grid-content');
      if(filesContent&&filesContent.parentNode){
        filesContent.parentNode.style.position='relative';
        filesContent.parentNode.appendChild(panel);
      }
    }
    return panel;
  }

  function _showDetailPanel(item){
    _selectedItem=item;
    var panel=_getDetailPanelEl();
    if(!panel) return;

    var ext=_getExt(item.name);
    var iconCls=_getIconClass(item.name);
    var isImage=_getCat(item.name)==='images'||(item.mimeType&&item.mimeType.indexOf('image')===0);
    var isGdrive=item.source==='gdrive';

    var html='<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">';
    html+='<span style="font-size:11px;color:var(--pb-text-dim);text-transform:uppercase;letter-spacing:0.05em">File Details</span>';
    html+='<button onclick="window._portalFiles.hideDetail()" style="background:none;border:none;color:var(--pb-text-dim);cursor:pointer;font-size:18px;padding:4px">&times;</button>';
    html+='</div>';

    // Icon + name
    html+='<div style="text-align:center;margin-bottom:20px">';
    if(isImage&&!isGdrive){
      html+='<div style="margin-bottom:10px;border-radius:8px;overflow:hidden;max-height:180px"><img src="/api/download?path='+encodeURIComponent(item.path)+'" style="max-width:100%;max-height:180px;object-fit:contain" onerror="this.parentNode.innerHTML=\'Preview unavailable\'"></div>';
    } else {
      html+='<div class="f-icon '+iconCls+'" style="width:48px;height:48px;margin:0 auto 8px;font-size:14px">'+_FILE_SVG+'</div>';
    }
    html+='<div style="font-size:14px;font-weight:600;color:var(--pb-text);word-break:break-word">'+_esc(item.name)+'</div>';
    html+='</div>';

    // Metadata table
    html+='<div style="font-size:12px;color:var(--pb-text-muted);margin-bottom:20px">';
    html+='<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--pb-border)"><span>Type</span><span>'+_getTypeLabel(item.name)+'</span></div>';
    if(item.size!=null) html+='<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--pb-border)"><span>Size</span><span>'+_fmtSize(item.size)+'</span></div>';
    if(item.mtime) html+='<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--pb-border)"><span>Modified</span><span>'+_fmtDate(item.mtime)+'</span></div>';
    if(item.path) html+='<div style="padding:6px 0;border-bottom:1px solid var(--pb-border)"><div>Path</div><div style="font-size:11px;color:var(--pb-text-dim);word-break:break-all;margin-top:2px">'+_esc(item.path)+'</div></div>';
    html+='</div>';

    // Preview area for text-based files
    var isPreviewable=!isGdrive&&!item.is_dir&&_isTextFile(item.name);
    if(isPreviewable){
      html+='<div id="file-preview-content" style="margin-bottom:16px;background:rgba(0,0,0,0.3);border:1px solid var(--pb-border);border-radius:6px;padding:10px;max-height:200px;overflow:auto;font-family:monospace;font-size:11px;color:var(--pb-text-muted);white-space:pre-wrap;word-break:break-all">Loading preview...</div>';
    }

    // Action buttons
    html+='<div style="display:flex;flex-direction:column;gap:8px">';
    if(!isGdrive&&!item.is_dir){
      html+='<button onclick="window._portalFiles.openPreviewModal()" style="width:100%;padding:10px;border:none;border-radius:6px;background:rgba(139,92,246,0.15);color:#a78bfa;font-size:12px;font-weight:600;cursor:pointer">Open Preview</button>';
    }
    if(isGdrive){
      var gMime=item.mimeType||'';
      if(gMime.indexOf('google-apps.document')>-1||gMime.indexOf('google-apps.spreadsheet')>-1||gMime.indexOf('google-apps.presentation')>-1){
        html+='<button onclick="if(window.gdriveOpenFile)window.gdriveOpenFile(\''+_esc(item.gdriveId||item.path)+'\',\''+_esc(gMime)+'\',\''+encodeURIComponent(item.name)+'\')" style="width:100%;padding:10px;border:none;border-radius:6px;background:rgba(66,133,244,0.15);color:#4285f4;font-size:12px;font-weight:600;cursor:pointer">Open in Google</button>';
      }
      html+='<button onclick="if(window.gdriveOpenFile)window.gdriveOpenFile(\''+_esc(item.gdriveId||item.path)+'\',\''+_esc(gMime)+'\',\''+encodeURIComponent(item.name)+'\')" style="width:100%;padding:10px;border:none;border-radius:6px;background:rgba(42,147,193,0.15);color:var(--pb-blue-light);font-size:12px;font-weight:600;cursor:pointer">Download</button>';
    } else {
      html+='<button onclick="window._portalDownload(\''+_esc(item.path)+'\')" style="width:100%;padding:10px;border:none;border-radius:6px;background:rgba(42,147,193,0.15);color:var(--pb-blue-light);font-size:12px;font-weight:600;cursor:pointer">Download</button>';
    }
    html+='<button onclick="window._portalFiles.deleteFile()" style="width:100%;padding:10px;border:none;border-radius:6px;background:rgba(239,68,68,0.1);color:#ef4444;font-size:12px;font-weight:600;cursor:pointer">Delete</button>';
    html+='</div>';

    panel.innerHTML=html;
    panel.style.display='block';

    // Load text preview
    if(isPreviewable){
      _loadFilePreview(item.path,'file-preview-content');
    }
  }

  // --- text file detection ---
  var _TEXT_EXTS=['txt','md','mdx','py','js','ts','jsx','tsx','html','htm','css','sh','bash',
    'yml','yaml','toml','json','xml','csv','tsv','sql','rs','go','java','c','cpp','h','rb',
    'ini','cfg','conf','env','log','gitignore','dockerfile','makefile','rst','tex'];
  function _isTextFile(name){
    var ext=_getExt(name);
    return _TEXT_EXTS.indexOf(ext)>-1;
  }

  // --- load file preview content ---
  function _loadFilePreview(filePath, targetElId){
    var token=_tok();
    if(!token) return;
    fetch('/api/download?path='+encodeURIComponent(filePath),{
      headers:{'Authorization':'Bearer '+token}
    }).then(function(r){
      if(!r.ok) throw new Error('HTTP '+r.status);
      var ct=r.headers.get('content-type')||'';
      if(ct.indexOf('text')>-1||ct.indexOf('json')>-1||ct.indexOf('javascript')>-1||ct.indexOf('xml')>-1||ct.indexOf('yaml')>-1){
        return r.text();
      }
      return r.text(); // try as text for known extensions
    }).then(function(text){
      var el=document.getElementById(targetElId);
      if(!el) return;
      // Truncate for side panel preview
      var maxLen=targetElId==='file-preview-content'?3000:50000;
      var truncated=text.length>maxLen;
      el.textContent=truncated?text.substring(0,maxLen)+'\n\n... (truncated)':text;
    }).catch(function(err){
      var el=document.getElementById(targetElId);
      if(el) el.textContent='Preview unavailable: '+err.message;
    });
  }

  // --- large preview modal ---
  function _getPreviewModalEl(){
    var modal=document.getElementById('file-preview-modal');
    if(!modal){
      modal=document.createElement('div');
      modal.id='file-preview-modal';
      modal.style.cssText='display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px)';
      modal.innerHTML='<div style="position:absolute;top:3%;left:5%;right:5%;bottom:3%;background:var(--pb-surface-alt,rgba(15,20,35,0.98));border:1px solid var(--pb-border);border-radius:12px;display:flex;flex-direction:column;overflow:hidden">'
        +'<div id="fpm-header" style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--pb-border);flex-shrink:0">'
        +'<span id="fpm-title" style="font-size:14px;font-weight:600;color:var(--pb-text)"></span>'
        +'<button onclick="window._portalFiles.closePreviewModal()" style="background:none;border:none;color:var(--pb-text-dim);cursor:pointer;font-size:22px;padding:4px 8px">&times;</button></div>'
        +'<div id="fpm-body" style="flex:1;overflow:auto;padding:20px"></div></div>';
      modal.addEventListener('click',function(e){ if(e.target===modal) _closePreviewModal(); });
      document.body.appendChild(modal);
    }
    return modal;
  }

  function _openPreviewModal(){
    if(!_selectedItem) return;
    var item=_selectedItem;
    var modal=_getPreviewModalEl();
    var title=document.getElementById('fpm-title');
    var body=document.getElementById('fpm-body');
    if(title) title.textContent=item.name;

    var isImage=_getCat(item.name)==='images';
    var isText=_isTextFile(item.name);
    var isGdrive=item.source==='gdrive';

    if(isGdrive){
      body.innerHTML='<div style="padding:40px;text-align:center;color:var(--pb-text-dim)">Preview not available for Google Drive files. Use "Open in Google" instead.</div>';
    } else if(isImage){
      body.innerHTML='<div style="text-align:center;padding:20px"><img src="/api/download?path='+encodeURIComponent(item.path)+'" style="max-width:100%;max-height:80vh;object-fit:contain;border-radius:8px" onerror="this.parentNode.innerHTML=\'Preview unavailable\'"></div>';
    } else if(isText){
      body.innerHTML='<pre id="fpm-preview-text" style="margin:0;font-family:monospace;font-size:13px;color:var(--pb-text-muted);white-space:pre-wrap;word-break:break-all;line-height:1.6">Loading...</pre>';
      _loadFilePreview(item.path,'fpm-preview-text');
    } else {
      body.innerHTML='<div style="padding:40px;text-align:center;color:var(--pb-text-dim)"><div style="font-size:48px;margin-bottom:16px">'+_FILE_SVG.replace('viewBox','style="width:64px;height:64px;fill:var(--pb-text-dim)" viewBox')+'</div>'
        +'<div style="font-size:14px;margin-bottom:8px">'+_esc(item.name)+'</div>'
        +'<div style="font-size:12px">Preview not available for this file type</div>'
        +'<button onclick="window._portalDownload(\''+_esc(item.path)+'\')" style="margin-top:16px;padding:10px 24px;border:none;border-radius:6px;background:rgba(42,147,193,0.15);color:var(--pb-blue-light);font-size:12px;font-weight:600;cursor:pointer">Download Instead</button></div>';
    }

    modal.style.display='block';
  }

  function _closePreviewModal(){
    var modal=document.getElementById('file-preview-modal');
    if(modal) modal.style.display='none';
  }

  // Close preview modal on Escape
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') _closePreviewModal(); });

  function _hideDetailPanel(){
    _selectedItem=null;
    var panel=document.getElementById('file-detail-panel');
    if(panel) panel.style.display='none';
  }

  function _deleteFile(){
    if(!_selectedItem) return;
    var item=_selectedItem;
    var msg='Are you sure you want to delete "'+item.name+'"?';
    if(!confirm(msg)) return;

    var token=_tok();
    if(!token) return;
    fetch('/api/files',{
      method:'DELETE',
      headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},
      body:JSON.stringify({path:item.path})
    }).then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){
        if(typeof showToast==='function') showToast('Deleted: '+item.name);
        _hideDetailPanel();
        _loadFiles(_currentDir);
      } else {
        if(typeof showToast==='function') showToast('Delete failed: '+(d.error||'unknown'));
      }
    }).catch(function(e){
      if(typeof showToast==='function') showToast('Delete error: '+e.message);
    });
  }

  // =========================================================================
  // FEATURE 3: Right-Click Context Menu
  // =========================================================================

  var _contextMenuEl=null;
  var _contextItem=null;

  function _getContextMenuEl(){
    if(!_contextMenuEl){
      _contextMenuEl=document.createElement('div');
      _contextMenuEl.id='files-context-menu';
      _contextMenuEl.style.cssText='display:none;position:fixed;z-index:9999;min-width:160px;background:rgba(15,20,35,0.96);border:1px solid var(--pb-border);border-radius:8px;padding:4px 0;backdrop-filter:blur(12px);box-shadow:0 8px 32px rgba(0,0,0,0.4)';
      document.body.appendChild(_contextMenuEl);
    }
    return _contextMenuEl;
  }

  function _showContextMenu(e, item){
    e.preventDefault();
    e.stopPropagation();
    _contextItem=item;
    var menu=_getContextMenuEl();
    var html='';
    if(!item.is_dir){
      html+='<div class="ctx-item" onclick="window._portalFiles._ctxDownload()">Download</div>';
    }
    if(item.is_dir){
      html+='<div class="ctx-item" onclick="window._portalFiles._ctxOpen()">Open</div>';
    } else if(item.source==='gdrive'){
      html+='<div class="ctx-item" onclick="window._portalFiles._ctxOpen()">Open in Google</div>';
    }
    html+='<div class="ctx-item" onclick="window._portalFiles._ctxCopyPath()">Copy path</div>';
    html+='<div class="ctx-sep"></div>';
    html+='<div class="ctx-item ctx-danger" onclick="window._portalFiles._ctxDelete()">Delete</div>';
    menu.innerHTML=html;
    // Position at cursor
    var x=e.clientX, y=e.clientY;
    menu.style.display='block';
    // Adjust if overflows viewport
    var mw=menu.offsetWidth, mh=menu.offsetHeight;
    if(x+mw>window.innerWidth) x=window.innerWidth-mw-8;
    if(y+mh>window.innerHeight) y=window.innerHeight-mh-8;
    menu.style.left=x+'px';
    menu.style.top=y+'px';
  }

  function _hideContextMenu(){
    if(_contextMenuEl) _contextMenuEl.style.display='none';
    _contextItem=null;
  }

  function _ctxDownload(){
    _hideContextMenu();
    if(!_contextItem||_contextItem.is_dir) return;
    if(_contextItem.source==='gdrive'){
      if(window.gdriveOpenFile) window.gdriveOpenFile(_contextItem.gdriveId||_contextItem.path, _contextItem.mimeType||'', encodeURIComponent(_contextItem.name));
    } else {
      window._portalDownload(_contextItem.path);
    }
  }

  function _ctxOpen(){
    _hideContextMenu();
    if(!_contextItem) return;
    if(_contextItem.is_dir){
      if(_contextItem.source==='gdrive'){
        if(window.gdriveOpenFolder) window.gdriveOpenFolder(_contextItem.gdriveId||_contextItem.path, _contextItem.name);
      } else {
        _navigate(_contextItem.path||_contextItem.name);
      }
    } else if(_contextItem.source==='gdrive'){
      if(window.gdriveOpenFile) window.gdriveOpenFile(_contextItem.gdriveId||_contextItem.path, _contextItem.mimeType||'', encodeURIComponent(_contextItem.name));
    }
  }

  function _ctxCopyPath(){
    _hideContextMenu();
    if(!_contextItem) return;
    var path=_contextItem.path||_contextItem.name;
    if(navigator.clipboard){
      navigator.clipboard.writeText(path).then(function(){
        if(typeof showToast==='function') showToast('Path copied');
      });
    }
  }

  function _ctxDelete(){
    _hideContextMenu();
    if(!_contextItem) return;
    _selectedItem=_contextItem;
    _deleteFile();
  }

  // Close context menu on outside click or Escape
  document.addEventListener('click',function(){ _hideContextMenu(); });
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') _hideContextMenu(); });

  // --- render grid + list ---
  function _renderView(){
    var items=_getFilteredItems();
    _renderGrid(items);
    _renderList(items);
    // update info bar
    var dirs=items.filter(function(it){return it.is_dir;}).length;
    var files=items.length-dirs;
    var parts=[];
    if(dirs>0) parts.push(dirs+' folder'+(dirs>1?'s':''));
    if(files>0) parts.push(files+' file'+(files>1?'s':''));
    if(_infoCount) _infoCount.textContent=parts.join(', ')||'Empty';
    if(_infoSort) _infoSort.textContent=_currentCat==='all'?'All types':_currentCat.charAt(0).toUpperCase()+_currentCat.slice(1);
  }

  function _buildCardHtml(item, idx){
    var dataIdx=' data-file-idx="'+idx+'"';
    if(item.is_dir){
      var onclick;
      if(item.source==='gdrive'){
        onclick="if(window.gdriveOpenFolder)window.gdriveOpenFolder('"+_esc(item.gdriveId||item.path)+"','"+_esc(item.name).replace(/'/g,"\\'")+"')";
      } else {
        onclick="if(window._portalFiles)window._portalFiles.navigate('"+_esc(item.path||item.name)+"')";
      }
      return '<div class="f-card"'+dataIdx+' ondblclick="'+onclick+'" oncontextmenu="window._portalFiles._onCtx(event,'+idx+')" style="cursor:pointer">'
        +'<div class="f-icon" style="background:transparent;border:none">'+_FOLDER_SVG+'</div>'
        +'<div class="f-name">'+_esc(item.name)+'</div>'
        +'<div class="f-meta">Folder'+(item._rootDir?' &middot; '+_esc(item.path):'')+'</div>'
        +'</div>';
    }
    var ext=_getExt(item.name);
    var iconCls=_getIconClass(item.name);
    var extLabel=ext?ext.toUpperCase():'';
    // Single click shows detail panel; double-click opens full preview; contextmenu for right-click
    var clickHandler="window._portalFiles._showDetailPanel("+idx+")";
    var dblClickHandler="window._portalFiles._openPreviewByIdx("+idx+")";
    return '<div class="f-card"'+dataIdx+' onclick="'+clickHandler+'" ondblclick="'+dblClickHandler+'" oncontextmenu="window._portalFiles._onCtx(event,'+idx+')" style="cursor:pointer" title="Click for details, double-click to preview">'
      +'<div class="f-icon '+iconCls+'">'+_FILE_SVG
      +(extLabel?'<span style="position:absolute;font-size:7px;font-weight:700;bottom:6px;color:var(--pb-text-muted)">'+_esc(extLabel)+'</span>':'')
      +'</div>'
      +'<div class="f-name">'+_esc(item.name)+'</div>'
      +'<div class="f-meta">'+_fmtSize(item.size)+'</div>'
      +'</div>';
  }

  function _renderGrid(items){
    if(!_gridContent) return;
    if(items.length===0){
      if(_currentDir===null && _allItems.length===0 && _source==='local'){
        _gridContent.innerHTML='<div style="padding:32px;text-align:center;color:var(--pb-text-dim);font-size:13px;line-height:1.8;">No files yet.<br>Upload files via chat or Telegram.<br>They will appear here for browsing and download.</div>';
      } else if(_currentCat!=='all'){
        _gridContent.innerHTML='<div style="padding:24px;text-align:center;color:var(--pb-text-dim);font-size:13px;">No '+_esc(_currentCat)+' files in this directory.</div>';
      } else {
        _gridContent.innerHTML='<div style="padding:24px;text-align:center;color:var(--pb-text-dim);font-size:13px;">This folder is empty.</div>';
      }
      return;
    }
    // If showing all categories, group into sections
    if(_currentCat==='all'){
      var html='';
      // Directories first
      var dirs=items.filter(function(it){return it.is_dir;});
      var files=items.filter(function(it){return !it.is_dir;});
      if(dirs.length>0){
        html+='<div class="fs-section" data-cat="folders"><div class="fs-header">'
          +'<svg viewBox="0 0 24 24" style="width:13px;height:13px;stroke:var(--pb-yellow);fill:none;stroke-width:2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
          +'<span>Folders</span><span class="fs-count">'+dirs.length+'</span></div><div class="files-grid">';
        dirs.forEach(function(d,i){ html+=_buildCardHtml(d,items.indexOf(d)); });
        html+='</div></div>';
      }
      // Group files by category
      var catOrder=['documents','markdown','code','images','data','other'];
      var catLabels={documents:'Documents',markdown:'Markdown',code:'Code',images:'Images',data:'Data',other:'Other'};
      var catColors={documents:'var(--pb-blue-light)',markdown:'#818cf8',code:'var(--pb-purple)',images:'var(--pb-green)',data:'var(--pb-yellow)',other:'var(--pb-text-dim)'};
      var grouped={};
      files.forEach(function(f){
        var cat=(f.source==='gdrive')?_getMimeCat(f.mimeType):_getCat(f.name);
        if(!grouped[cat]) grouped[cat]=[];
        grouped[cat].push(f);
      });
      catOrder.forEach(function(cat){
        if(!grouped[cat]||grouped[cat].length===0) return;
        var catSvg=cat==='images'?'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>'
          :cat==='code'?'<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>'
          :'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>';
        html+='<div class="fs-section" data-cat="'+_esc(cat)+'"><div class="fs-header">'
          +'<svg viewBox="0 0 24 24" style="width:13px;height:13px;stroke:'+catColors[cat]+';fill:none;stroke-width:2">'+catSvg+'</svg>'
          +'<span>'+_esc(catLabels[cat]||cat)+'</span><span class="fs-count">'+grouped[cat].length+'</span></div><div class="files-grid">';
        grouped[cat].forEach(function(f){ html+=_buildCardHtml(f,items.indexOf(f)); });
        html+='</div></div>';
      });
      _gridContent.innerHTML=html;
    } else {
      // Single category view
      var html='<div class="files-grid">';
      items.forEach(function(f,i){ html+=_buildCardHtml(f,i); });
      html+='</div>';
      _gridContent.innerHTML=html;
    }
  }

  function _fmtDate(ts){
    if(!ts) return '--';
    var d;
    if(typeof ts==='string'){
      d=new Date(ts);
    } else {
      d=new Date(ts*1000);
    }
    var months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[d.getMonth()]+' '+d.getDate()+', '+d.getFullYear();
  }

  function _renderList(items){
    if(!_listContent) return;
    if(items.length===0){
      _listContent.innerHTML='<div style="padding:20px;text-align:center;color:var(--pb-text-dim);font-size:13px;">No files to show.</div>';
      return;
    }
    var html='';
    items.forEach(function(item,idx){
      if(item.is_dir){
        var onclick;
        if(item.source==='gdrive'){
          onclick="if(window.gdriveOpenFolder)window.gdriveOpenFolder('"+_esc(item.gdriveId||item.path)+"','"+_esc(item.name).replace(/'/g,"\\'")+"')";
        } else {
          onclick="if(window._portalFiles)window._portalFiles.navigate('"+_esc(item.path||item.name)+"')";
        }
        html+='<div class="fl-row" ondblclick="'+onclick+'" oncontextmenu="window._portalFiles._onCtx(event,'+idx+')" style="cursor:pointer">'
          +'<div class="fl-name"><div class="f-icon" style="background:transparent;border:none">'+_FOLDER_SVG.replace('32px','16px').replace('32px','16px')+'</div><span>'+_esc(item.name)+'</span></div>'
          +'<div class="fl-cell">--</div><div class="fl-cell">--</div><div class="fl-cell">DIR</div></div>';
      } else {
        var iconCls=_getIconClass(item.name);
        html+='<div class="fl-row" onclick="window._portalFiles._showDetailPanel('+idx+')" ondblclick="window._portalFiles._openPreviewByIdx('+idx+')" oncontextmenu="window._portalFiles._onCtx(event,'+idx+')" style="cursor:pointer" title="Click for details, double-click to preview">'
          +'<div class="fl-name"><div class="f-icon '+iconCls+'">'+_FILE_SVG+'</div><span>'+_esc(item.name)+'</span></div>'
          +'<div class="fl-cell">'+_fmtSize(item.size)+'</div>'
          +'<div class="fl-cell">'+_fmtDate(item.mtime)+'</div>'
          +'<div class="fl-cell">'+_getTypeLabel(item.name)+'</div></div>';
      }
    });
    _listContent.innerHTML=html;
  }

  // --- quick nav (root dir shortcuts) ---
  function _updateQuickNav(){
    var el=document.getElementById('ftQuickNavItems');
    if(!el||_rootDirs.length===0) return;
    var html='';
    _rootDirs.forEach(function(rd){
      var name=rd.split('/').pop()||rd;
      html+='<div class="ft-item" onclick="if(window._portalFiles)window._portalFiles.navigate(\''+_esc(rd)+'\')" style="cursor:pointer">'
        +'<svg viewBox="0 0 24 24" style="fill:none;stroke:currentColor;stroke-width:2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
        +'<span class="ft-label">'+_esc(name)+'</span></div>';
    });
    el.innerHTML=html;
  }

  // --- sidebar counts ---
  function _updateSidebarCounts(){
    var counts={documents:0,markdown:0,code:0,images:0,audio:0,video:0,data:0,folders:0};
    _allItems.forEach(function(it){
      if(it.is_dir){ counts.folders++; return; }
      var cat=(it.source==='gdrive')?_getMimeCat(it.mimeType):_getCat(it.name);
      if(counts[cat]!==undefined) counts[cat]++;
    });
    ['documents','markdown','code','images','audio','video','data','folders'].forEach(function(cat){
      var el=document.getElementById('ft-count-'+cat);
      if(el) el.textContent=counts[cat];
    });
  }

  // --- storage bar ---
  function _updateStorageBar(){
    var totalBytes=0;
    _allItems.forEach(function(it){
      if(!it.is_dir && it.size) totalBytes+=it.size;
    });
    var el=document.getElementById('files-storage-text');
    var fill=document.getElementById('files-storage-fill');
    if(el) el.textContent=_fmtSize(totalBytes)+' visible';
    if(fill) fill.style.width='0%'; // No quota info from API; just show used
  }

  // --- file upload ---
  function _handleUpload(fileList){
    if(!fileList||fileList.length===0) return;
    var token=_tok();
    if(!token){ if(typeof showToast==='function') showToast('Not authenticated'); return; }

    var total=fileList.length;
    var done=0;
    if(typeof showToast==='function') showToast('Uploading '+total+' file'+(total>1?'s':'')+'...');

    for(var i=0;i<fileList.length;i++){
      (function(file){
        // 50MB limit check
        if(file.size>50*1024*1024){
          if(typeof showToast==='function') showToast('File too large (max 50MB): '+file.name);
          done++;
          if(done>=total) _onUploadsDone();
          return;
        }
        var fd=new FormData();
        fd.append('file',file);
        fetch('/api/chat/upload',{
          method:'POST',
          headers:{'Authorization':'Bearer '+token},
          body:fd
        })
        .then(function(r){ return r.json(); })
        .then(function(d){
          done++;
          if(d.ok){
            if(done>=total) _onUploadsDone();
          } else {
            if(typeof showToast==='function') showToast('Upload failed: '+(d.error||'unknown'));
            if(done>=total) _onUploadsDone();
          }
        })
        .catch(function(err){
          done++;
          if(typeof showToast==='function') showToast('Upload error: '+String(err));
          if(done>=total) _onUploadsDone();
        });
      })(fileList[i]);
    }

    // Reset the input so same file can be re-uploaded
    var inp=document.getElementById('localFileUploadInput');
    if(inp) inp.value='';
  }

  function _onUploadsDone(){
    if(typeof showToast==='function') showToast('Upload complete. Refreshing...');
    // Uploaded files go to ~/portal_uploads — navigate there so user sees them
    var uploadsDir=_rootDirs.find(function(d){ return d.indexOf('portal_uploads')>-1; });
    setTimeout(function(){ _loadFiles(uploadsDir||_currentDir); },800);
  }

  // --- search ---
  function _initSearch(){
    var searchInput=document.querySelector('#filesArea .files-search input');
    if(!searchInput) return;
    searchInput.addEventListener('input',function(){
      _searchQuery=this.value.trim();
      _renderView();
    });
  }

  // --- navigate ---
  function _navigate(path){
    _searchQuery='';
    var searchInput=document.querySelector('#filesArea .files-search input');
    if(searchInput) searchInput.value='';
    _loadFiles(path);
  }

  // --- go up ---
  function _goUp(){
    if(_source==='gdrive'){
      // Let the gdrive code handle going up
      if(typeof window._gdriveGoUp==='function') window._gdriveGoUp();
      return;
    }
    if(!_currentDir) return; // already at root
    var parts=_currentDir.split('/').filter(Boolean);
    if(parts.length<=1){ _loadFiles(null); return; }
    parts.pop();
    var parent='/'+parts.join('/');
    // Check if parent is still within an allowed root dir, otherwise go to root
    var isAllowed=_rootDirs.some(function(rd){ return parent===rd || parent.indexOf(rd+'/')===0; });
    if(!isAllowed){ _loadFiles(null); return; }
    _loadFiles(parent);
  }

  // --- boot ---
  function _boot(){
    _initRefs();
    _initSearch();
    _initContextMenuStyles();
    if(_tok()) _loadFiles(null);
  }

  // --- inject context menu CSS ---
  function _initContextMenuStyles(){
    if(document.getElementById('files-ctx-styles')) return;
    var style=document.createElement('style');
    style.id='files-ctx-styles';
    style.textContent='.ctx-item{padding:7px 14px;font-size:12px;color:var(--pb-text-muted);cursor:pointer;transition:background .1s}'
      +'.ctx-item:hover{background:rgba(255,255,255,0.06);color:var(--pb-text)}'
      +'.ctx-danger{color:#ef4444}'
      +'.ctx-danger:hover{background:rgba(239,68,68,0.1);color:#ef4444}'
      +'.ctx-sep{height:1px;margin:4px 8px;background:var(--pb-border)}';
    document.head.appendChild(style);
  }

  // Listen for auth events
  window.addEventListener('portal-auth',function(){ setTimeout(_boot,500); });
  // Auto-boot on page load if token exists
  if(_tok()) setTimeout(_boot,2000);

  // Internal handlers for indexed items
  function _onCtx(e,idx){
    var items=_getFilteredItems();
    if(items[idx]) _showContextMenu(e, items[idx]);
  }
  function _showDetailByIdx(idx){
    var items=_getFilteredItems();
    if(items[idx]) _showDetailPanel(items[idx]);
  }
  function _openPreviewByIdx(idx){
    var items=_getFilteredItems();
    if(items[idx]){
      _selectedItem=items[idx];
      _openPreviewModal();
    }
  }

  // Expose API
  window._portalFiles={
    load:function(){ _boot(); },
    navigate:_navigate,
    goUp:_goUp,
    filterCategory:_filterCategory,
    handleUpload:_handleUpload,
    setItems:_setItems,
    setGdriveBreadcrumb:_setGdriveBreadcrumb,
    getSource:function(){ return _source; },
    setSource:function(s){ _source=s; },
    hideDetail:_hideDetailPanel,
    deleteFile:_deleteFile,
    _showDetailPanel:_showDetailByIdx,
    _openPreviewByIdx:_openPreviewByIdx,
    openPreviewModal:_openPreviewModal,
    closePreviewModal:_closePreviewModal,
    _onCtx:_onCtx,
    _ctxDownload:_ctxDownload,
    _ctxOpen:_ctxOpen,
    _ctxCopyPath:_ctxCopyPath,
    _ctxDelete:_ctxDelete
  };
  // Secure download via fetch + blob (avoids token in URL)
  window._portalDownload=function(filePath){
    var token=_tok();
    fetch('/api/download?path='+encodeURIComponent(filePath),{
      headers:{'Authorization':'Bearer '+token}
    }).then(function(r){
      if(!r.ok)throw new Error('Download failed: '+r.status);
      return r.blob();
    }).then(function(blob){
      var url=URL.createObjectURL(blob);
      var a=document.createElement('a');
      a.href=url;a.download=filePath.split('/').pop()||'download';
      document.body.appendChild(a);a.click();
      setTimeout(function(){URL.revokeObjectURL(url);a.remove();},100);
    }).catch(function(e){
      if(typeof showToast==='function')showToast('Download error: '+e.message);
    });
  };
})();

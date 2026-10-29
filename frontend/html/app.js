// ตรวจจับ Host อัตโนมัติ: Production ใช้ Render Backend, Local ใช้ localhost
const API_URL = (() => {
    const host = window.location.hostname;
    // กรณี local development
    if (host === 'localhost' || host === '127.0.0.1') {
        if (window.location.port === '80' || window.location.port === '') {
            return '';
        }
        return `${window.location.protocol}//${host}:8000`;
    }
    // กรณี Cloudflare tunnel (เดิม)
    if (host.includes('trycloudflare.com')) {
        return '';
    }
    // กรณี Production (Vercel/Netlify) → ชี้ไป Render Backend
    return 'https://roommatch-9ohk.onrender.com';
})();

// ============================================================
// Warm-up: Ping backend ตอนโหลดหน้า เพื่อลด Cold Start delay
// ============================================================
(function warmUpBackend() {
    if (API_URL) {
        fetch(API_URL + '/health', { method: 'GET', signal: AbortSignal.timeout(90000) })
            .then(r => r.json())
            .then(d => console.log('[RoomMatch] Backend ready:', d))
            .catch(e => console.warn('[RoomMatch] Backend warm-up:', e.message));
    }
})();

// ==========================================
// 1. Session Management
// ==========================================
const Session = {
    save: (userData) => {
        try {
            const prevUser = Session.load();
            const prevUname = prevUser?.username;
            const newUname = userData?.username;
            // ถ้าสลับผู้ใช้คนใหม่ (ต่างคนกัน) ให้ล้างข้อมูลการออกแบบของคนเดิมออก
            if (prevUname && newUname && prevUname !== newUname) {
                try {
                    sessionStorage.removeItem('room_design_data');
                } catch (_) {}
            }
            localStorage.setItem('user_session', JSON.stringify(userData));
        } catch (e) {
            console.error('LocalStorage error:', e);
        }
    },
    load: () => {
        try {
            const data = localStorage.getItem('user_session');
            return data ? JSON.parse(data) : null;
        } catch (e) {
            return null;
        }
    },
    clear: () => {
        try {
            localStorage.removeItem('user_session');
            sessionStorage.removeItem('room_design_data');
            sessionStorage.clear();
        } catch (_) {}
    },
    isLoggedIn: () => !!localStorage.getItem('user_session'),
    getRole: () => {
        try {
            const data = localStorage.getItem('user_session');
            if (!data) return 'guest';
            const parsed = JSON.parse(data);
            return parsed.role || 'guest';
        } catch (e) {
            return 'guest';
        }
    }
};

// ==========================================
// 2. UI Helpers (Alerts & Spinners)
// ==========================================
function showAlert(message, type = 'success') {
    const container = document.getElementById('alert-container');
    if (!container) return;

    const alertEl = document.createElement('div');
    const bgColor = type === 'success' ? 'bg-green-100 border-green-400 text-green-700' : 'bg-red-100 border-red-400 text-red-700';
    const icon = type === 'success' ? '<i class="fa-solid fa-circle-check"></i>' : '<i class="fa-solid fa-circle-exclamation"></i>';

    alertEl.className = `flex items-center gap-3 px-4 py-3 rounded-lg border shadow-sm ${bgColor} transform transition-all duration-300 translate-x-full opacity-0 z-50`;
    alertEl.innerHTML = `${icon} <span class="font-medium">${message}</span>`;

    container.appendChild(alertEl);

    setTimeout(() => alertEl.classList.remove('translate-x-full', 'opacity-0'), 10);
    setTimeout(() => {
        alertEl.classList.add('opacity-0');
        setTimeout(() => alertEl.remove(), 300);
    }, 3000);
}

function toggleLoading(isLoading) {
    const overlay = document.getElementById('loading-overlay');
    if (!overlay) return;
    if (isLoading) overlay.classList.add('active');
    else overlay.classList.remove('active');
}

function avatarUrl(username, size) {
    const nameStr = username || 'User';
    const params = new URLSearchParams({ name: nameStr, background: '0D8ABC', color: 'fff' });
    if (size) params.set('size', size);
    return `https://ui-avatars.com/api/?${params.toString()}`;
}

// ==========================================
// 3. API Wrapper 
// ==========================================
async function apiFetch(path, options = {}) {
    try {
        const user = Session.load();
        const headers = { ...(options.headers || {}) };
        
        const isFormData = options.body instanceof FormData;
        const method = (options.method || 'GET').toUpperCase();
        
        if (!isFormData && !headers['Content-Type'] && method !== 'GET') {
            headers['Content-Type'] = 'application/json';
        }
        
        const currentUserId = user ? (user.id || user.user_id) : null;
        if (currentUserId) {
            if (!headers['x-user-id']) headers['x-user-id'] = String(currentUserId);
            if (!headers['user-id']) headers['user-id'] = String(currentUserId);
        }

        const response = await fetch(`${API_URL}${path}`, {
            ...options,
            headers
        });

        const contentType = response.headers.get('content-type');
        let data = {};
        
        if (contentType && contentType.includes('application/json')) {
            try {
                data = await response.json();
            } catch (err) {
                data = {};
            }
        } else if (!response.ok) {
            throw new Error(`ไม่สามารถเชื่อมต่อระบบได้ (${response.status})`);
        }

        if (!response.ok) {
            throw new Error(data.detail || data.message || 'เกิดข้อผิดพลาดในการเชื่อมต่อ');
        }
        return data;
    } catch (error) {
        console.error('API Error:', error);
        throw error;
    }
}

// ==========================================
// 3.5 บันทึกการคลิกสั่งซื้อ (backend เก็บลงตาราง product_events)
// ==========================================
function recordStoreClick(productId) {
    if (!productId) return;
    try {
        const user = Session.load();
        const uid = user ? (user.id || user.user_id) : null;
        const headers = {};
        if (uid) headers['x-user-id'] = String(uid);

        // ลิงก์เปิดในแท็บใหม่ หน้านี้ยังอยู่ จึงส่งได้ตามปกติ และไม่ให้ error กระทบการเปิดลิงก์
        fetch(`${API_URL}/api/products/${productId}/click`, { method: 'POST', headers })
            .catch(() => {});
    } catch (e) {
        console.error('recordStoreClick error:', e);
    }
}

// ดักคลิกปุ่มสั่งซื้อทุกปุ่มที่มี data-buy-product-id (ทั้งรายการทั้งหมดและสินค้าที่ AI แนะนำ)
document.addEventListener('click', (e) => {
    const link = e.target.closest ? e.target.closest('a[data-buy-product-id]') : null;
    if (link) recordStoreClick(link.dataset.buyProductId);
});

// ==========================================
// 4. ระบบจัดการสินค้า (Seller & Admin) 
// ==========================================
function loadSellerStats(products) {
    const statTotal = document.getElementById('stat-total-products');
    const statAi = document.getElementById('stat-ai-usage');
    const statStore = document.getElementById('stat-store-clicks');
    
    const prodArray = Array.isArray(products) ? products : [];

    if (statTotal) statTotal.textContent = prodArray.length;
    
    if (statAi) {
        statAi.textContent = prodArray.reduce((sum, p) => sum + (p.ai_usage || 0), 0);
    }
    if (statStore) {
        statStore.textContent = prodArray.reduce((sum, p) => sum + (p.store_clicks || 0), 0);
    }
}

async function loadSellerProducts() {
    const tbody = document.getElementById('product-table-body') || document.getElementById('seller-products-table');
    if (!tbody) return;

    try {
        const user = Session.load();
        const userId = user ? (user.id || user.user_id || '') : '';
        
        const responseData = await apiFetch('/api/products', {
            headers: {
                'x-user-id': String(userId)
            }
        });
        
        const products = Array.isArray(responseData) ? responseData : (responseData.data || []);
        
        loadSellerStats(products);
        tbody.innerHTML = '';

        if (!products || products.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="p-8 text-center text-slate-500">ยังไม่มีสินค้าในร้านค้าของคุณ</td></tr>`;
            return;
        }

        const html = products.map(product => {
            const imgSrc = product.image_url || product.image_base64 || 'https://via.placeholder.com/150';
            const priceTh = Number(product.price || 0).toLocaleString('th-TH');
            const hasUrl = product.product_url && String(product.product_url).trim() !== 'null' && String(product.product_url).trim() !== '';
            
            const isActive = product.is_active !== false; 
            const statusBadge = isActive 
                ? `<span class="px-3 py-1 bg-green-100 text-green-700 text-xs font-bold rounded-full"><i class="fa-solid fa-eye mr-1"></i>ใช้งานอยู่</span>`
                : `<span class="px-3 py-1 bg-slate-100 text-slate-500 text-xs font-bold rounded-full"><i class="fa-solid fa-eye-slash mr-1"></i>ซ่อนอยู่</span>`;

            const toggleBtn = isActive
                ? `<button onclick="toggleProductStatus('${product.id}', false)" class="text-slate-400 hover:text-orange-500 px-2 cursor-pointer border-none bg-transparent" title="ซ่อนสินค้า"><i class="fa-solid fa-eye-slash"></i></button>`
                : `<button onclick="toggleProductStatus('${product.id}', true)" class="text-slate-400 hover:text-green-500 px-2 cursor-pointer border-none bg-transparent" title="เปิดใช้งานสินค้า"><i class="fa-solid fa-eye"></i></button>`;

            return `
                <tr class="seller-product-row border-b border-slate-100 hover:bg-slate-50 transition" data-status="${isActive ? 'active' : 'hidden'}">
                    <td class="p-4">
                        <div class="w-12 h-12 bg-slate-200 rounded-lg overflow-hidden">
                            <img src="${imgSrc}" class="w-full h-full object-cover" alt="Product" onerror="this.src='https://via.placeholder.com/150'">
                        </div>
                    </td>
                    <td class="p-4">
                        <p class="font-medium text-slate-800">${product.name || 'ไม่มีชื่อสินค้า'}</p>
                        <span class="text-xs text-slate-400">ร้าน: ${product.seller_name || '-'}</span>
                        ${product.style_tags ? `<span class="ml-2 px-2 py-0.5 bg-blue-100 text-blue-600 text-[10px] rounded-full">${product.style_tags}</span>` : ''}
                    </td>
                    <td class="p-4">
                        <p class="text-slate-700">${product.category || '-'}</p>
                        ${hasUrl ? `<a href="${product.product_url}" target="_blank" class="text-xs text-blue-500 hover:underline"><i class="fa-solid fa-link"></i> ดูหน้าร้าน</a>` : `<span class="text-xs text-slate-400">ไม่มีลิงก์</span>`}
                    </td>
                    <td class="p-4 text-slate-700 font-medium">${priceTh} ฿</td>
                    <td class="p-4 text-center">${statusBadge}</td>
                    <td class="p-4 text-center">
                        ${toggleBtn}
                        <button onclick="deleteProduct('${product.id}')" class="text-red-500 hover:text-red-700 px-2 cursor-pointer border-none bg-transparent" title="ลบ"><i class="fa-solid fa-trash"></i></button>
                    </td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = html;
        applyCurrentStatusFilter();

    } catch (error) {
        console.error('Error loading products:', error);
        tbody.innerHTML = `<tr><td colspan="6" class="p-8 text-center text-red-500">เกิดข้อผิดพลาดในการโหลดข้อมูล</td></tr>`;
    }
}

window.toggleProductStatus = async function(productId, newStatus) {
    if (!Session.isLoggedIn()) return showAlert('กรุณาเข้าสู่ระบบ', 'error');

    try {
        await apiFetch(`/api/products/${productId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ is_active: newStatus })
        });
        showAlert(newStatus ? 'เปิดใช้งานสินค้าแล้ว' : 'ซ่อนสินค้าแล้ว', 'success');
        
        const adminModal = document.getElementById('admin-products-modal');
        if (adminModal && !adminModal.classList.contains('hidden')) {
            loadAdminProducts();
        } else {
            loadSellerProducts();
        }
    } catch (error) {
        showAlert(error.message, 'error');
    }
};

function applyCurrentStatusFilter() {
    const activeBtn = document.querySelector('.status-filter-btn.bg-blue-600') || document.querySelector('.status-filter-btn.active');
    if (!activeBtn) return;
    
    const filter = (activeBtn.getAttribute('data-status-filter') || activeBtn.textContent.trim()).toLowerCase();
    const rows = document.querySelectorAll('.seller-product-row');
    
    rows.forEach(row => {
        const rowStatus = row.getAttribute('data-status'); 
        
        if (filter === 'all' || filter === 'ทั้งหมด') {
            row.style.display = '';
        } else if ((filter === 'active' || filter.includes('ใช้งาน')) && rowStatus === 'active') {
            row.style.display = '';
        } else if ((filter === 'hidden' || filter.includes('ซ่อน')) && rowStatus === 'hidden') {
            row.style.display = '';
        } else {
            row.style.display = 'none';
        }
    });
}

function setupStatusFilter() {
    const filterBtns = document.querySelectorAll('.status-filter-btn');
    if (!filterBtns.length) return;

    filterBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            filterBtns.forEach(b => {
                b.className = 'status-filter-btn px-5 py-2 bg-white text-slate-600 border border-slate-200 hover:bg-slate-50 rounded-full font-medium whitespace-nowrap transition cursor-pointer';
                b.classList.remove('active');
            });
            btn.className = 'status-filter-btn px-5 py-2 bg-blue-600 text-white rounded-full font-medium whitespace-nowrap shadow-sm border-none cursor-pointer active';
            applyCurrentStatusFilter();
        });
    });
}

async function loadAdminProducts() {
    const tbody = document.getElementById('admin-products-tbody');
    if (!tbody) return;

    try {
        toggleLoading(true);
        const user = Session.load();
        const userId = user ? (user.id || user.user_id || '') : '';

        const responseData = await apiFetch('/api/products', {
            headers: { 'x-user-id': String(userId) }
        });
        const products = Array.isArray(responseData) ? responseData : (responseData.data || []);
        
        tbody.innerHTML = '';

        if (!products || products.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-slate-500">ยังไม่มีข้อมูลสินค้าในระบบ</td></tr>`;
            return;
        }

        const html = products.map(product => {
            const imgSrc = product.image_url || product.image_base64 || 'https://via.placeholder.com/150';
            const priceTh = Number(product.price || 0).toLocaleString('th-TH');
            
            const hasUrl = product.product_url && String(product.product_url).trim() !== 'null' && String(product.product_url).trim() !== '';
            const productLink = hasUrl ? product.product_url : '#';
            const linkTarget = hasUrl ? 'target="_blank"' : '';
            
            const isActive = product.is_active !== false;
            const statusBadge = isActive 
                ? `<span class="px-2 py-1 bg-green-100 text-green-700 text-[10px] font-bold rounded-full">ใช้งาน</span>`
                : `<span class="px-2 py-1 bg-slate-100 text-slate-500 text-[10px] font-bold rounded-full">ซ่อน</span>`;

            const toggleBtn = isActive
                ? `<button onclick="toggleProductStatus('${product.id}', false)" class="text-slate-400 hover:text-orange-500 hover:bg-orange-50 p-2 rounded-lg transition border-none cursor-pointer" title="ซ่อนสินค้า"><i class="fa-solid fa-eye-slash"></i></button>`
                : `<button onclick="toggleProductStatus('${product.id}', true)" class="text-slate-400 hover:text-green-500 hover:bg-green-50 p-2 rounded-lg transition border-none cursor-pointer" title="เปิดใช้งานสินค้า"><i class="fa-solid fa-eye"></i></button>`;

            return `
                <tr class="border-b border-slate-100 hover:bg-slate-50 transition">
                    <td class="p-3 text-slate-500">${product.id}</td>
                    <td class="p-3">
                        <a href="${productLink}" ${linkTarget} class="block w-12 h-12 bg-slate-200 rounded-lg overflow-hidden cursor-pointer hover:opacity-80 transition" title="${hasUrl ? 'คลิกเพื่อดูสินค้า' : 'ไม่มีลิงก์สินค้า'}">
                            <img src="${imgSrc}" class="w-full h-full object-cover" alt="Product" onerror="this.src='https://via.placeholder.com/150'">
                        </a>
                    </td>
                    <td class="p-3">
                        <a href="${productLink}" ${linkTarget} class="font-bold text-slate-800 hover:text-blue-600 transition block mb-1" title="${hasUrl ? 'คลิกเพื่อดูสินค้า' : 'ไม่มีลิงก์สินค้า'}">${product.name || 'ไม่มีชื่อสินค้า'}</a>
                        <span class="text-xs text-slate-400">ร้านค้า: ${product.seller_name || '-'}</span>
                    </td>
                    <td class="p-3 text-slate-700">${product.category || '-'}</td>
                    <td class="p-3 text-slate-700 font-medium">${priceTh} ฿</td>
                    <td class="p-3 text-center">${statusBadge}</td>
                    <td class="p-3 text-center">
                        ${toggleBtn}
                        <button onclick="deleteProduct('${product.id}')" class="text-red-500 hover:text-red-700 hover:bg-red-50 p-2 rounded-lg transition border-none cursor-pointer" title="ลบสินค้า">
                            <i class="fa-solid fa-trash"></i>
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = html;

    } catch (error) {
        console.error('Error loading admin products:', error);
        tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-red-500">เกิดข้อผิดพลาด: ${error.message}</td></tr>`;
    } finally {
        toggleLoading(false);
    }
}

window.deleteProduct = async function(productId) {
    if(!confirm('คุณแน่ใจหรือไม่ว่าต้องการลบสินค้านี้?')) return;
    if (!Session.isLoggedIn()) return showAlert('กรุณาเข้าสู่ระบบก่อนทำการลบสินค้า', 'error');

    try {
        await apiFetch(`/api/products/${productId}`, { method: 'DELETE' });
        showAlert('ลบสินค้าสำเร็จ', 'success');
        
        const adminModal = document.getElementById('admin-products-modal');
        if (adminModal && !adminModal.classList.contains('hidden')) {
            loadAdminProducts();
        } else {
            loadSellerProducts(); 
        }
    } catch (error) {
        showAlert(error.message, 'error');
    }
}

// ==========================================
// 5. ระบบจัดการผู้ใช้งานและภาพรวมระบบ (Admin)
// ==========================================
async function loadAdminUsers() {
    const tbody = document.getElementById('admin-users-tbody') || document.getElementById('admin-users-table-body');
    if (!tbody) return;

    try {
        toggleLoading(true);
        const responseData = await apiFetch('/api/users'); 
        const users = Array.isArray(responseData) ? responseData : (responseData.data || []);
        
        tbody.innerHTML = '';
        if (!users || users.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="p-8 text-center text-slate-500">ไม่มีข้อมูลผู้ใช้งาน</td></tr>`;
            return;
        }

        const html = users.map(u => {
            const roleColor = u.role === 'admin' ? 'bg-red-100 text-red-700 font-medium' : (u.role === 'seller' ? 'bg-orange-100 text-orange-700 font-medium' : 'bg-slate-100 text-slate-700 font-medium');
            
            const isActive = u.is_active !== false;
            const statusBadge = isActive 
                ? '<span class="px-3 py-1 bg-green-100 text-green-700 text-xs font-bold rounded-full">ใช้งานปกติ</span>'
                : '<span class="px-3 py-1 bg-red-100 text-red-700 text-xs font-bold rounded-full">ถูกระงับ</span>';

            const toggleStatusBtn = isActive
                ? `<button onclick="toggleUserStatus('${u.id}', '${u.role}', false)" class="text-orange-500 hover:text-orange-700 hover:bg-orange-50 p-2 rounded-lg transition border-none cursor-pointer bg-transparent" title="ระงับบัญชี"><i class="fa-solid fa-ban"></i></button>`
                : `<button onclick="toggleUserStatus('${u.id}', '${u.role}', true)" class="text-green-500 hover:text-green-700 hover:bg-green-50 p-2 rounded-lg transition border-none cursor-pointer bg-transparent" title="ปลดระงับบัญชี"><i class="fa-solid fa-check-circle"></i></button>`;

            return `
                <tr class="border-b border-slate-100 hover:bg-slate-50 transition">
                    <td class="p-3 text-slate-500">${u.id}</td>
                    <td class="p-3">
                        <div class="font-bold text-slate-800">${u.username}</div>
                        <div class="text-xs text-slate-500">${u.email || '-'}</div>
                    </td>
                    <td class="p-3">
                        <select onchange="changeUserRole('${u.id}', this.value, ${isActive})" class="text-sm border border-slate-200 rounded-lg px-3 py-1.5 outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer ${roleColor}">
                            <option value="customer" ${u.role === 'customer' || u.role === 'user' ? 'selected' : ''}>Customer (ลูกค้า)</option>
                            <option value="seller" ${u.role === 'seller' ? 'selected' : ''}>Seller (ร้านค้า)</option>
                            <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin (ผู้ดูแลระบบ)</option>
                        </select>
                    </td>
                    <td class="p-3 text-center">${statusBadge}</td>
                    <td class="p-3 text-center">
                        ${toggleStatusBtn}
                        <button onclick="deleteUser('${u.id}')" class="text-red-500 hover:text-red-700 hover:bg-red-50 p-2 rounded-lg transition border-none cursor-pointer bg-transparent" title="ลบผู้ใช้งาน">
                            <i class="fa-solid fa-trash"></i>
                        </button>
                    </td>
                </tr>
            `;
        }).join('');
        
        tbody.innerHTML = html;
    } catch (error) {
        console.error('Error loading users:', error);
        tbody.innerHTML = `<tr><td colspan="5" class="p-8 text-center text-red-500">เกิดข้อผิดพลาด: ${error.message}</td></tr>`;
    } finally {
        toggleLoading(false);
    }
}

window.changeUserRole = async function(userId, newRole, currentStatus) {
    if (!confirm(`ยืนยันการเปลี่ยนสิทธิ์ผู้ใช้นี้เป็น ${newRole} ใช่หรือไม่?`)) {
        loadAdminUsers();
        return;
    }
    
    try {
        toggleLoading(true);
        await apiFetch(`/api/users/${userId}`, {
            method: 'PUT',
            body: JSON.stringify({ role: newRole, is_active: currentStatus })
        });
        showAlert('อัปเดตสิทธิ์ผู้ใช้งานสำเร็จ', 'success');
        loadAdminUsers(); 
    } catch (error) {
        showAlert(error.message, 'error');
        loadAdminUsers();
    } finally {
        toggleLoading(false);
    }
}

window.toggleUserStatus = async function(userId, currentRole, newStatus) {
    const actionText = newStatus ? 'ปลดระงับ' : 'ระงับ';
    if (!confirm(`ยืนยันการ${actionText}ผู้ใช้งานรายนี้?`)) return;
    
    try {
        toggleLoading(true);
        await apiFetch(`/api/users/${userId}`, {
            method: 'PUT',
            body: JSON.stringify({ role: currentRole, is_active: newStatus })
        });
        showAlert(`อัปเดตสถานะผู้ใช้งานสำเร็จ`, 'success');
        loadAdminUsers(); 
    } catch (error) {
        showAlert(error.message, 'error');
    } finally {
        toggleLoading(false);
    }
}

window.deleteUser = async function(userId) {
    if (!confirm('ระวัง! คุณแน่ใจหรือไม่ว่าต้องการลบผู้ใช้งานรายนี้? (การลบจะไม่สามารถกู้คืนได้)')) return;
    
    try {
        toggleLoading(true);
        await apiFetch(`/api/users/${userId}`, { method: 'DELETE' });
        showAlert('ลบผู้ใช้งานออกจากระบบสำเร็จ', 'success');
        loadAdminUsers(); 
    } catch (error) {
        showAlert(error.message, 'error');
    } finally {
        toggleLoading(false);
    }
}

async function loadAdminOverview() {
    try {
        toggleLoading(true);
        
        const stats = await apiFetch('/api/admin/stats');

        const statUsers = document.getElementById('stat-total-users');
        const statProducts = document.getElementById('stat-total-sys-products');
        const statAi = document.getElementById('stat-total-ai');
        const statClicks = document.getElementById('stat-total-clicks');

        if (statUsers) statUsers.textContent = stats.total_users || 0;
        if (statProducts) statProducts.textContent = stats.total_products || 0;
        if (statAi) statAi.textContent = stats.ai_usage || 0;
        if (statClicks) statClicks.textContent = stats.store_clicks || 0;

        const statCustomer = document.getElementById('stat-role-customer');
        const statSeller = document.getElementById('stat-role-seller');
        const statAdmin = document.getElementById('stat-role-admin');

        if (statCustomer) statCustomer.textContent = (stats.roles && stats.roles.customer) || 0;
        if (statSeller) statSeller.textContent = (stats.roles && stats.roles.seller) || 0;
        if (statAdmin) statAdmin.textContent = (stats.roles && stats.roles.admin) || 0;

    } catch (error) {
        console.error('Error loading overview stats:', error);
        showAlert('ไม่สามารถโหลดข้อมูลสถิติได้', 'error');
    } finally {
        toggleLoading(false);
    }
}

// ==========================================
// 6. ฟังก์ชันควบคุม UI และสิทธิ์ (Role)
// ==========================================
function checkUserRoles() {
    const role = Session.getRole();

    document.querySelectorAll('.admin-only, .seller-only').forEach(el => {
        el.style.display = 'none';
    });

    if (role === 'admin') {
        document.querySelectorAll('.admin-only').forEach(el => el.style.display = 'block');
    } else if (role === 'seller') {
        document.querySelectorAll('.seller-only').forEach(el => el.style.display = 'block');
    }

    const sellerNavMenus = document.querySelectorAll('.nav-orders, #nav-orders'); 
    const adminNavMenus = document.querySelectorAll('.nav-dashboard, #nav-dashboard');

    sellerNavMenus.forEach(menu => {
        menu.style.setProperty('display', role === 'seller' ? 'inline-block' : 'none', 'important'); 
    });

    adminNavMenus.forEach(menu => {
        menu.style.setProperty('display', role === 'admin' ? 'inline-block' : 'none', 'important');
    });
}

function updateNavbar() {
    const user = Session.load();
    const userProfile = document.getElementById('user-profile');
    const loginBtn = document.getElementById('login-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const navUsername = document.getElementById('nav-username');
    const navAvatar = document.getElementById('nav-avatar');
    const heroCtaBtn = document.getElementById('hero-cta-btn');

    if (user && userProfile) {
        if(loginBtn) loginBtn.classList.add('hidden');
        userProfile.classList.remove('hidden');
        userProfile.classList.add('flex');
        if(logoutBtn) logoutBtn.classList.remove('hidden');
        if(navUsername) navUsername.textContent = user.username;
        if(navAvatar) navAvatar.src = user.avatar_url || avatarUrl(user.username);

        if(heroCtaBtn) {
            heroCtaBtn.textContent = 'เริ่มออกแบบห้อง';
            heroCtaBtn.href = 'upload.html';
        }
    } else if (userProfile) {
        if(loginBtn) loginBtn.classList.remove('hidden');
        userProfile.classList.add('hidden');
        userProfile.classList.remove('flex');
        if(logoutBtn) logoutBtn.classList.add('hidden');

        if(heroCtaBtn) {
            heroCtaBtn.textContent = 'สมัครสมาชิกเลย';
            heroCtaBtn.href = 'register.html';
        }
    }

    checkUserRoles();
}

function handleLogout() {
    Session.clear();
    try {
        sessionStorage.removeItem('room_design_data');
        sessionStorage.clear();
    } catch (_) {}
    updateNavbar();
    showAlert('ออกจากระบบแล้ว', 'success');
    setTimeout(() => window.location.href = 'index.html', 800);
}

function switchView(viewId) {
    if (viewId === 'login') {
        if (typeof openLoginModal === 'function') {
            openLoginModal();
            return;
        }
        const modal = document.getElementById('login-modal');
        if (modal) {
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            const card = modal.querySelector('.modal-card');
            if (card) {
                card.classList.remove('scale-95', 'opacity-0');
                card.classList.add('scale-100', 'opacity-100');
            }
            return;
        }
    }
    document.querySelectorAll('.page-view').forEach(page => page.classList.remove('active'));
    const target = document.getElementById('page-' + viewId);
    if(target) target.classList.add('active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function requireAuthOrRedirect() {
    const currentPage = window.location.pathname.split('/').pop() || 'index.html';
    const role = Session.getRole();
    const protectedPages = ['upload.html', 'result.html', 'dashboard.html', 'orders.html', 'profile.html'];
    const isProtected = protectedPages.includes(currentPage) || document.body.dataset.requiresAuth === 'true';

    if (isProtected && !Session.isLoggedIn()) {
        sessionStorage.setItem('pendingAlert', 'กรุณาเข้าสู่ระบบก่อนใช้งานฟีเจอร์นี้ครับ');
        window.location.replace('index.html?action=login');
        return false;
    }

    if (currentPage === 'dashboard.html' && role !== 'admin') {
        sessionStorage.setItem('pendingAlert', 'คุณไม่มีสิทธิ์เข้าถึงหน้าจัดการระบบครับ (สำหรับ Admin เท่านั้น)');
        window.location.replace('index.html');
        return false;
    }

    if (currentPage === 'orders.html' && role !== 'seller') {
        sessionStorage.setItem('pendingAlert', 'คุณไม่มีสิทธิ์เข้าถึงหน้าจัดการคลังสินค้าครับ');
        window.location.replace('index.html');
        return false;
    }

    return true;
}

// ==========================================
// 6.5 ส่วนโหลดสินค้าฝั่ง Customer
// ==========================================
async function loadStoreProducts() {
    const container = document.getElementById('all-products-container'); 
    if (!container) return;

    try {
        toggleLoading(true);
        const response = await fetch(`${API_URL}/api/products`);
        if (!response.ok) throw new Error('ไม่สามารถดึงข้อมูลสินค้าได้');
        
        const responseData = await response.json();
        const products = Array.isArray(responseData) ? responseData : (responseData.data || []);
        const activeProducts = products.filter(p => p.is_active !== false);

        if (!activeProducts || activeProducts.length === 0) {
            container.innerHTML = '<div class="col-span-full text-center text-slate-500 py-8">ยังไม่มีสินค้าในระบบ</div>';
            return;
        }

        const html = activeProducts.map(p => {
            const imgSrc = p.image_url || p.image_base64 || 'https://via.placeholder.com/300';
            const priceTh = Number(p.price || 0).toLocaleString('th-TH');
            const isValidUrl = p.product_url && String(p.product_url).trim() !== 'null' && String(p.product_url).trim() !== '';
            
            let dataCategory = p.category || 'อื่นๆ';
            
            const buyBtn = isValidUrl
                ? `<a href="${p.product_url}" target="_blank" data-buy-product-id="${p.id}" class="px-4 py-2 bg-gradient-to-r from-orange-500 to-amber-500 hover:from-orange-600 hover:to-amber-600 text-white text-xs font-bold rounded-xl transition-all flex items-center gap-1.5 border-none cursor-pointer shadow-sm shadow-orange-500/20 hover:-translate-y-0.5"><i class="fa-solid fa-cart-shopping text-[11px]"></i> สั่งซื้อ</a>`
                : `<button disabled class="px-4 py-2 bg-slate-100 text-slate-400 text-xs font-medium rounded-xl cursor-not-allowed">ไม่มีลิงก์</button>`;

            return `
                <div class="product-card group bg-white rounded-3xl border border-slate-200/80 shadow-xs hover:shadow-xl hover:shadow-slate-200/60 transition-all duration-300 hover:-translate-y-1 flex flex-col justify-between overflow-hidden" data-category="${dataCategory}">
                    <div>
                        <div class="relative overflow-hidden bg-slate-100 aspect-square">
                            <img src="${imgSrc}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" alt="Product" onerror="this.src='https://via.placeholder.com/600'">
                            ${p.style_tags ? `<span class="absolute top-3 left-3 bg-slate-900/80 text-white text-[11px] px-2.5 py-1 rounded-full font-semibold backdrop-blur-md shadow-xs">${p.style_tags}</span>` : ''}
                        </div>
                        <div class="p-5">
                            <span class="text-[11px] text-blue-600 font-bold uppercase tracking-wider block mb-1">${p.category || 'ทั่วไป'}</span>
                            <h3 class="font-bold text-slate-800 text-base leading-snug group-hover:text-blue-600 transition-colors line-clamp-2">${p.name || 'ไม่มีชื่อสินค้า'}</h3>
                            <p class="text-xs text-slate-500 line-clamp-2 mt-1.5">${p.description || ''}</p>
                        </div>
                    </div>
                    <div class="p-5 pt-0">
                        <div class="flex items-center justify-between pt-3 border-t border-slate-100">
                            <div>
                                <span class="text-[10px] text-slate-400 font-medium block">ราคา</span>
                                <span class="text-lg font-extrabold text-slate-900">฿${priceTh}</span>
                            </div>
                            ${buyBtn}
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = html;
        setupCategoryFilters();

    } catch (error) {
        console.error('Error loading store products:', error);
        container.innerHTML = `<div class="col-span-full text-center text-red-500 py-8">เกิดข้อผิดพลาดในการโหลดสินค้า</div>`;
    } finally {
        toggleLoading(false);
    }
}

// ==========================================
// 6.6 จัดการหมวดหมู่สินค้าฝั่ง Customer (แก้ไขเพื่อป้องกันการคลิกชนกับการ์ดสินค้า)
// ==========================================
function setupCategoryFilters() {
    // เลือกเฉพาะปุ่มหมวดหมู่ ไม่ดึง [data-category] ของการ์ดสินค้ามาผูกอีเวนต์คลิก
    const filterButtons = document.querySelectorAll('.category-filter');
    if (filterButtons.length === 0) return;

    filterButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            const selectedCat = btn.dataset.category || btn.textContent.trim();
            if (!selectedCat) return;

            filterButtons.forEach(b => {
                b.classList.remove('bg-blue-600', 'text-white');
                b.classList.add('bg-white', 'text-slate-600', 'hover:bg-slate-50');
            });
            
            btn.classList.remove('bg-white', 'text-slate-600', 'hover:bg-slate-50');
            btn.classList.add('bg-blue-600', 'text-white');

            const productCards = document.querySelectorAll('.product-card');

            productCards.forEach(card => {
                const cardCat = card.dataset.category || '';
                if (selectedCat === 'all' || selectedCat === 'ทั้งหมด' || cardCat.includes(selectedCat) || selectedCat.includes(cardCat)) {
                    card.style.display = 'flex';
                } else {
                    card.style.display = 'none';
                }
            });
        });
    });
}

// ==========================================
// 6.7 ส่วนโหลดข้อมูลหน้า Profile
// ==========================================
function loadUserProfile() {
    const idEl = document.getElementById('profile-id') || document.querySelector('[data-field="id"]');
    const usernameEl = document.getElementById('profile-username') || document.querySelector('[data-field="username"]');
    const emailEl = document.getElementById('profile-email') || document.querySelector('[data-field="email"]');
    
    if (!usernameEl && !idEl) return;

    const user = Session.load();
    if (!user) {
        showAlert('ไม่พบข้อมูลผู้ใช้งาน กรุณาเข้าสู่ระบบใหม่', 'error');
        setTimeout(() => window.location.href = 'index.html', 1500);
        return;
    }

    if (idEl) idEl.textContent = user.id || user.user_id || '-';
    if (usernameEl) usernameEl.textContent = user.username || '-';
    if (emailEl) emailEl.textContent = user.email || '-';

    const avatarImg = document.getElementById('profile-avatar') || document.getElementById('profile-avatar-img');
    if (avatarImg) {
        avatarImg.src = user.avatar_url || avatarUrl(user.username, 128);
    }
}

// ==========================================
// 7. Event Listeners หลักของระบบ
// ==========================================
document.addEventListener('DOMContentLoaded', () => {

    if (!requireAuthOrRedirect()) return;

    updateNavbar();
    loadUserProfile(); // เรียกใช้ฟังก์ชันดึงข้อมูลโปรไฟล์

    if (Session.getRole() === 'seller') {
        loadSellerProducts(); 
        setupStatusFilter(); 
    }
    
    if (document.getElementById('all-products-container')) {
        loadStoreProducts();
    }

    const addProductModal = document.getElementById('add-product-modal');
    const openAddModalBtn = document.getElementById('open-add-modal-btn');
    const closeModalBtn = document.getElementById('close-modal-btn');

    if (openAddModalBtn && addProductModal) {
        openAddModalBtn.addEventListener('click', () => addProductModal.classList.remove('hidden'));
    }

    if (closeModalBtn && addProductModal) {
        closeModalBtn.addEventListener('click', () => addProductModal.classList.add('hidden'));
    }

    // ==========================================
    // เพิ่ม: จัดการการแสดงตัวอย่างรูปภาพใน Modal เพิ่มสินค้า
    // ==========================================
    const productImageInput = document.getElementById('product-image');
    const imagePreviewContainer = document.getElementById('image-preview-container');
    const imagePreviewElement = document.getElementById('image-preview-element');

    if (productImageInput && imagePreviewContainer && imagePreviewElement) {
        productImageInput.addEventListener('change', function(event) {
            const file = event.target.files[0];
            
            if (file) {
                if (file.type.startsWith('image/')) {
                    const reader = new FileReader();
                    
                    reader.onload = function(e) {
                        imagePreviewElement.src = e.target.result;
                        imagePreviewContainer.classList.remove('hidden'); 
                    }
                    reader.readAsDataURL(file);
                } else {
                    showAlert('กรุณาอัปโหลดไฟล์รูปภาพเท่านั้น', 'error');
                    productImageInput.value = '';
                    imagePreviewContainer.classList.add('hidden');
                    imagePreviewElement.src = '#';
                }
            } else {
                imagePreviewContainer.classList.add('hidden');
                imagePreviewElement.src = '#';
            }
        });
    }

    const adminUsersModal = document.getElementById('admin-users-modal');
    const btnOpenUsersModal = document.getElementById('btn-open-users-modal');
    const closeUsersModal = document.getElementById('close-users-modal');

    if (btnOpenUsersModal && adminUsersModal) {
        btnOpenUsersModal.addEventListener('click', () => {
            adminUsersModal.classList.remove('hidden');
            adminUsersModal.classList.add('flex');
            loadAdminUsers();
        });
    }

    if (closeUsersModal && adminUsersModal) {
        closeUsersModal.addEventListener('click', () => {
            adminUsersModal.classList.add('hidden');
            adminUsersModal.classList.remove('flex');
        });
    }

    const adminProductsModal = document.getElementById('admin-products-modal');
    const btnOpenProductsModal = document.getElementById('btn-open-products-modal');
    const closeProductsModal = document.getElementById('close-products-modal');

    if (btnOpenProductsModal && adminProductsModal) {
        btnOpenProductsModal.addEventListener('click', () => {
            adminProductsModal.classList.remove('hidden');
            adminProductsModal.classList.add('flex');
            loadAdminProducts();
        });
    }

    if (closeProductsModal && adminProductsModal) {
        closeProductsModal.addEventListener('click', () => {
            adminProductsModal.classList.add('hidden');
            adminProductsModal.classList.remove('flex');
        });
    }

    const adminOverviewModal = document.getElementById('admin-overview-modal');
    const btnOpenOverviewModal = document.getElementById('btn-open-overview-modal');
    const closeOverviewModal = document.getElementById('close-overview-modal');

    if (btnOpenOverviewModal && adminOverviewModal) {
        btnOpenOverviewModal.addEventListener('click', () => {
            adminOverviewModal.classList.remove('hidden');
            adminOverviewModal.classList.add('flex');
            loadAdminOverview();
        });
    }

    if (closeOverviewModal && adminOverviewModal) {
        closeOverviewModal.addEventListener('click', () => {
            adminOverviewModal.classList.add('hidden');
            adminOverviewModal.classList.remove('flex');
        });
    }

    window.addEventListener('click', (e) => {
        if (addProductModal && e.target === addProductModal) {
            addProductModal.classList.add('hidden');
        }
        if (adminUsersModal && e.target === adminUsersModal) {
            adminUsersModal.classList.add('hidden');
            adminUsersModal.classList.remove('flex');
        }
        if (adminProductsModal && e.target === adminProductsModal) {
            adminProductsModal.classList.add('hidden');
            adminProductsModal.classList.remove('flex');
        }
        if (adminOverviewModal && e.target === adminOverviewModal) {
            adminOverviewModal.classList.add('hidden');
            adminOverviewModal.classList.remove('flex');
        }
    });

    const addProductForm = document.getElementById('add-product-form');
    if (addProductForm) {
        addProductForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const imgFileEl = document.getElementById('product-image');
            const imageFile = imgFileEl ? imgFileEl.files[0] : null;
            
            const user = Session.load();
            if (!user) return showAlert('กรุณาเข้าสู่ระบบก่อนทำรายการ', 'error');

            toggleLoading(true);

            try {
                const formData = new FormData();
                formData.append('name', document.getElementById('prod-name')?.value || document.getElementById('product-name')?.value || '');
                formData.append('category', document.getElementById('prod-category')?.value || document.getElementById('product-category')?.value || '');
                formData.append('price', document.getElementById('prod-price')?.value || document.getElementById('product-price')?.value || '');
                formData.append('description', document.getElementById('product-description')?.value || '');
                formData.append('product_url', document.getElementById('product-url')?.value || '');
                
                // เพิ่มการส่งค่า style_tags ไปยัง Backend
                formData.append('style_tags', document.getElementById('product-style')?.value || '');

                if (imageFile) {
                    formData.append('image', imageFile);
                } else {
                    const imgUrlEl = document.getElementById('prod-image');
                    const imgUrl = imgUrlEl ? imgUrlEl.value : '';
                    formData.append('image_url', imgUrl);
                }

                await apiFetch('/api/products', {
                    method: 'POST',
                    body: formData
                });

                if(addProductModal) addProductModal.classList.add('hidden');
                addProductForm.reset();
                
                // ซ่อน Image Preview หลังจากบันทึกสินค้าสำเร็จ
                if (imagePreviewContainer) imagePreviewContainer.classList.add('hidden');
                if (imagePreviewElement) imagePreviewElement.src = '#';
                
                showAlert('เพิ่มสินค้าสำเร็จ!', 'success');
                
                if (adminProductsModal && !adminProductsModal.classList.contains('hidden')) {
                    loadAdminProducts();
                } else {
                    loadSellerProducts(); 
                }

            } catch (error) {
                showAlert(error.message, 'error');
            } finally {
                toggleLoading(false);
            }
        });
    }

    const pendingAlert = sessionStorage.getItem('pendingAlert');
    if (pendingAlert) {
        showAlert(pendingAlert, 'error');
        sessionStorage.removeItem('pendingAlert');
    }

    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('action') === 'login' && typeof switchView === 'function') {
        switchView('login');
    }

    const userProfile = document.getElementById('user-profile');
    if (userProfile) {
        userProfile.classList.add('cursor-pointer', 'hover:opacity-80', 'transition-opacity');
        userProfile.onclick = (e) => {
            e.preventDefault();
            e.stopPropagation();
            window.location.href = 'profile.html';
        };
    }

    const logoutButtons = document.querySelectorAll('#logout-btn, .logout-btn');
    logoutButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            handleLogout();
        });
    });

    const loginForm = document.getElementById('login-form');
    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('login-username')?.value || '';
            const password = document.getElementById('login-password')?.value || '';
            const btn = document.getElementById('btn-submit-login');
            const spinner = document.getElementById('login-spinner');

            if(btn) btn.disabled = true;
            if(spinner) spinner.classList.remove('hidden');

            try {
                const data = await apiFetch('/login', {
                    method: 'POST',
                    body: JSON.stringify({ username, password })
                });

                Session.save(data.user || data); 
                showAlert('เข้าสู่ระบบสำเร็จ!', 'success');
                setTimeout(() => window.location.href = 'index.html', 1000); 

            } catch (error) {
                showAlert(error.message, 'error');
                if(btn) btn.disabled = false;
                if(spinner) spinner.classList.add('hidden');
            }
        });
    }

    const registerForm = document.getElementById('register-form');
    if (registerForm) {
        registerForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('reg-username')?.value || '';
            const email = document.getElementById('reg-email')?.value || '';
            const password = document.getElementById('reg-password')?.value || '';
            const confirm = document.getElementById('reg-confirm')?.value || '';
            
            const roleElement = document.getElementById('reg-role');
            const role = roleElement ? roleElement.value : 'customer';

            if (password !== confirm) {
                showAlert('รหัสผ่านไม่ตรงกัน', 'error');
                return;
            }

            const btn = document.getElementById('btn-submit-register');
            const spinner = document.getElementById('reg-spinner');

            if(btn) btn.disabled = true;
            if(spinner) spinner.classList.remove('hidden');

            try {
                await apiFetch('/register', {
                    method: 'POST',
                    body: JSON.stringify({ username, email, password, role })
                });
                showAlert('ลงทะเบียนสำเร็จ! กำลังพากลับไปหน้าแรก...', 'success');
                setTimeout(() => window.location.href = 'index.html', 1500);
            } catch (error) {
                showAlert(error.message, 'error');
                if(btn) btn.disabled = false;
                if(spinner) spinner.classList.add('hidden');
            }
        });
    }

    // ==========================================
    // 8. ส่วนแสดงผลลัพธ์ (Result Page) และ AI Design
    // ==========================================
    const budgetRange = document.getElementById('budget-range');
    const budgetValue = document.getElementById('budget-value');
    if (budgetRange && budgetValue) {
        budgetRange.addEventListener('input', () => {
            budgetValue.textContent = Number(budgetRange.value).toLocaleString('th-TH');
        });
    }

    // หมายเหตุ: การส่งแบบฟอร์มออกแบบห้อง (upload.html) ได้รับการจัดการด้วยระบบ AI Pipeline สมบูรณ์ใน upload.html แล้ว

    const resultPage = document.getElementById('result-page');
    if (resultPage) {
        let designData = {};
        const sessionUser = Session.load();
        const currentUname = sessionUser ? sessionUser.username : null;
        const currentUid = sessionUser ? (sessionUser.id || sessionUser.user_id) : null;

        try {
            designData = JSON.parse(sessionStorage.getItem('room_design_data') || '{}');
        } catch (e) {
            designData = {};
        }

        const resStyle = document.getElementById('result-style');
        const resBudget = document.getElementById('result-budget');
        const resImage = document.getElementById('result-image');

        if (resStyle && designData.style) resStyle.textContent = designData.style;
        // แปลงงบเป็นตัวเลขล้วน รองรับทั้ง 5000 และข้อความเก่า "5,000 บาท"
        const budgetNum = Number(String(designData.budget ?? '').replace(/[^\d.]/g, ''));
        if (resBudget && budgetNum > 0) resBudget.textContent = budgetNum.toLocaleString('th-TH') + ' บาท';
        if (resImage && designData.image) resImage.src = designData.image;

        async function fetchMatchedProducts(base64Image) {
            try {
                if (!base64Image) return;
                const res = await fetch(base64Image);
                const blob = await res.blob();
                const fileType = blob.type || "image/jpeg";
                const file = new File([blob], "room.jpg", { type: fileType });
                
                const formData = new FormData();
                formData.append('file', file);

                // ส่ง budget ไปยัง Backend
                if (budgetNum > 0) {
                    formData.append('budget', String(budgetNum));
                }
                
                const resultData = await apiFetch('/api/match-room', {
                    method: 'POST',
                    body: formData
                });
                
                const matchedProducts = Array.isArray(resultData) ? resultData : (resultData.data || []);
                renderProductCards(matchedProducts);

            } catch(err) {
                console.error("Match error:", err);
                showAlert('เกิดข้อผิดพลาดในการวิเคราะห์และค้นหาสินค้า', 'error');
            }
        }
        
        function renderProductCards(products, customPlacements) {
            const container = document.getElementById('matched-products-container'); 
            if (!container) return;

            if (!products || products.length === 0) {
                container.innerHTML = '<p class="text-slate-500 text-sm col-span-full">ไม่พบสินค้าที่อยู่ในงบประมาณของคุณ</p>';
                return;
            }

            const currentPlacements = customPlacements || ((designData && Array.isArray(designData.placements)) ? designData.placements : []);

            const html = products.map((p, idx) => {
                const isValidUrl = p.product_url && String(p.product_url).trim() !== 'null' && String(p.product_url).trim() !== '';
                const buyBtn = isValidUrl
                    ? `<a href="${p.product_url}" target="_blank" data-buy-product-id="${p.id}" class="w-full block text-center bg-gradient-to-r from-orange-500 to-amber-500 hover:from-orange-600 hover:to-amber-600 text-white py-2.5 rounded-xl transition-all font-bold text-sm shadow-md shadow-orange-500/20 hover:shadow-orange-500/35 hover:-translate-y-0.5"><i class="fa-solid fa-cart-shopping mr-2"></i>สั่งซื้อสินค้า</a>`
                    : `<button disabled class="w-full block text-center bg-slate-100 text-slate-400 py-2.5 rounded-xl cursor-not-allowed font-medium text-xs">ไม่มีลิงก์สั่งซื้อ</button>`;
                    
                const styleBadge = p.style_tags ? `<span class="bg-slate-900/80 text-white text-xs px-3 py-1 rounded-full absolute top-3 right-3 font-semibold shadow-md backdrop-blur-md">${p.style_tags}</span>` : '';
                const imgSrc = p.image_url || p.image_base64 || 'https://via.placeholder.com/300';
                
                // หาหมุดที่ตรงกับสินค้านี้
                const pin = currentPlacements.find(pl => pl.product_id === p.id);
                const pinNum = pin ? pin.pin_number : (idx + 1);
                const pinBadge = pin ? `<span class="bg-blue-600 hover:bg-blue-700 text-white text-xs px-3 py-1 rounded-full absolute top-3 left-3 font-extrabold shadow-md flex items-center gap-1.5 z-10 cursor-pointer backdrop-blur-sm transition" onclick="if(window.highlightPin) window.highlightPin(${p.id})"><i class="fa-solid fa-location-dot text-[10px]"></i> หมุดที่ ${pinNum}</span>` : '';
                    
                return `
                    <div class="product-card group bg-white rounded-3xl border border-slate-200/80 shadow-xs hover:shadow-2xl hover:shadow-slate-200/70 overflow-hidden relative flex flex-col h-full transition-all duration-300 hover:-translate-y-1.5 cursor-pointer" 
                         data-category="${p.category || 'อื่นๆ'}" 
                         data-product-id="${p.id}"
                         onmouseenter="if(window.highlightPin) window.highlightPin(${p.id})"
                         onmouseleave="if(window.resetPinHighlight) window.resetPinHighlight()">
                        ${pinBadge}
                        ${styleBadge}
                        <div class="w-full h-52 overflow-hidden bg-slate-100 relative">
                            <img src="${imgSrc}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" onerror="this.src='https://via.placeholder.com/300'">
                        </div>
                        <div class="p-5 sm:p-6 flex flex-col flex-grow justify-between">
                            <div>
                                <span class="text-[11px] font-bold text-blue-600 uppercase tracking-wider block mb-1">${p.category || 'เฟอร์นิเจอร์'}</span>
                                <h3 class="font-bold text-slate-800 text-base line-clamp-2 leading-snug group-hover:text-blue-600 transition-colors">${p.name || '-'}</h3>
                            </div>
                            <div class="mt-4 pt-3 border-t border-slate-100">
                                <div class="flex items-baseline justify-between mb-3">
                                    <span class="text-xs text-slate-400 font-medium">ราคาพิเศษ</span>
                                    <div class="flex items-baseline gap-1">
                                        <span class="text-xl sm:text-2xl font-extrabold text-slate-900">${Number(p.price || 0).toLocaleString('th-TH')}</span>
                                        <span class="text-xs font-bold text-slate-400">฿</span>
                                    </div>
                                </div>
                                ${buyBtn}
                            </div>
                        </div>
                    </div>
                `;
            }).join('');

            container.innerHTML = html; 
        }

        // เปิดให้หน้าผลลัพธ์อื่นเรียกใช้ได้โดยตรง
        window.renderProductCards = renderProductCards;

        // ใช้สินค้าที่ Ollama เลือกและ backend ตรวจงบแล้วก่อน ให้ตรงกับข้อความคำแนะนำ
        if (Array.isArray(designData.matchedProducts) && designData.matchedProducts.length > 0) {
            renderProductCards(designData.matchedProducts, designData.placements);
        } else if (designData.aiRecommendation) {
            renderProductCards([]);
        } else if (designData.image) {
            fetchMatchedProducts(designData.image);
        } else {
            // ลองดึงจาก Cloud Sync อัตโนมัติเผื่อเปิดจากคนละเครื่องหรือรีเฟรชหน้าจอ
            (async () => {
                try {
                    const headers = {};
                    if (currentUid) headers['x-user-id'] = String(currentUid);
                    const cloudRes = await fetch('/api/latest-room-design', { headers });
                    if (cloudRes.ok) {
                        const cloudData = await cloudRes.json();
                        if (cloudData && Array.isArray(cloudData.matchedProducts) && cloudData.matchedProducts.length > 0) {
                            renderProductCards(cloudData.matchedProducts, cloudData.placements);
                            return;
                        }
                    }
                } catch (_) {}
                const container = document.getElementById('matched-products-container');
                if (container) {
                    container.innerHTML = '<p class="text-slate-500 text-sm col-span-full">ยังไม่มีผลลัพธ์การออกแบบจาก AI (ไปที่เมนู "ออกแบบห้อง" เพื่อเริ่มต้น)</p>';
                }
            })();
        }
    }
});

window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
        requireAuthOrRedirect();
    }
});

// ==========================================
// 9. ระบบสมัครสมาชิกและเข้าสู่ระบบด้วย Google (Google Authentication)
// ==========================================

// Google OAuth 2.0 Client ID จาก Google Cloud Console
window.GOOGLE_CLIENT_ID = '336310861118-60ov1kutrdklgj71bkl0akh244kktiar.apps.googleusercontent.com';

// โหลด Google Identity Services script อัตโนมัติถ้ายังไม่มี
if (!document.getElementById('google-gsi-script')) {
    const gsiScript = document.createElement('script');
    gsiScript.id = 'google-gsi-script';
    gsiScript.src = 'https://accounts.google.com/gsi/client';
    gsiScript.async = true;
    gsiScript.defer = true;
    document.head.appendChild(gsiScript);
}

let currentGoogleAuthSource = 'login'; // 'login' หรือ 'register'

function ensureGoogleModal() {
    let modal = document.getElementById('google-auth-modal');
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = 'google-auth-modal';
    modal.className = 'fixed inset-0 z-50 bg-slate-950/60 backdrop-blur-md hidden items-center justify-center p-4 transition-all duration-300';
    modal.innerHTML = `
        <div class="bg-white max-w-[460px] w-full p-6 sm:p-8 rounded-3xl shadow-2xl border border-slate-200/80 relative overflow-hidden transform transition-all duration-300 scale-95 opacity-0 google-modal-card">
            <!-- แถบสี 4 สี Google ด้านบน -->
            <div class="absolute top-0 left-0 right-0 h-1.5 flex">
                <div class="h-full w-1/4 bg-[#4285F4]"></div>
                <div class="h-full w-1/4 bg-[#EA4335]"></div>
                <div class="h-full w-1/4 bg-[#FBBC05]"></div>
                <div class="h-full w-1/4 bg-[#34A853]"></div>
            </div>

            <!-- ปุ่มปิด Modal (X) -->
            <button type="button" onclick="closeGoogleModal()" class="absolute top-4 right-4 w-9 h-9 rounded-full bg-slate-100 hover:bg-slate-200 text-slate-500 hover:text-slate-800 flex items-center justify-center transition border-none cursor-pointer text-sm" title="ปิดหน้าต่าง">
                <i class="fa-solid fa-xmark"></i>
            </button>

            <!-- Header -->
            <div class="flex items-center gap-3.5 mb-6 pt-1">
                <div class="w-11 h-11 rounded-2xl bg-slate-50 border border-slate-200/80 flex items-center justify-center shadow-xs flex-shrink-0">
                    <svg class="w-6 h-6" viewBox="0 0 24 24">
                        <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                        <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                        <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/>
                        <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/>
                    </svg>
                </div>
                <div>
                    <h3 id="google-modal-title" class="text-lg sm:text-xl font-bold text-slate-900 tracking-tight leading-tight">ลงชื่อเข้าใช้ด้วย Google</h3>
                    <p class="text-xs text-slate-500 mt-0.5">เพื่อดำเนินการต่อไปยัง RoomMatch</p>
                </div>
            </div>

            <!-- Role Indicator -->
            <div id="google-modal-role-pill" class="mb-4 inline-flex items-center gap-1.5 px-3 py-1 bg-blue-50 text-blue-700 text-xs font-semibold rounded-full border border-blue-100">
                <i class="fa-solid fa-user-tag text-[10px]"></i>
                <span id="google-modal-role-text">บทบาท: ผู้ใช้งานทั่วไป (Customer)</span>
            </div>

            <!-- Fast Accounts Quick Picker -->
            <div class="flex flex-col gap-2.5 mb-5">
                <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-0.5">เลือกบัญชี Google เพื่อทดสอบทันที:</p>
                
                <button type="button" onclick="selectQuickGoogleAccount('thananchai.m@gmail.com', 'Thananchai Moomak', 'https://ui-avatars.com/api/?name=Thananchai+Moomak&background=4285F4&color=fff')" class="w-full flex items-center justify-between p-3 rounded-2xl border border-slate-200/90 hover:border-blue-400 hover:bg-blue-50/40 transition text-left cursor-pointer group bg-slate-50/50">
                    <div class="flex items-center gap-3">
                        <img src="https://ui-avatars.com/api/?name=Thananchai+Moomak&background=4285F4&color=fff" class="w-9 h-9 rounded-full object-cover border border-slate-200">
                        <div class="flex flex-col">
                            <span class="text-sm font-bold text-slate-800 group-hover:text-blue-600 transition">Thananchai Moomak</span>
                            <span class="text-xs text-slate-500">thananchai.m@gmail.com</span>
                        </div>
                    </div>
                    <i class="fa-solid fa-arrow-right text-xs text-slate-400 group-hover:text-blue-600 group-hover:translate-x-1 transition-transform"></i>
                </button>

                <button type="button" onclick="selectQuickGoogleAccount('korn.roommatch@gmail.com', 'Korn', 'https://ui-avatars.com/api/?name=Korn&background=34A853&color=fff')" class="w-full flex items-center justify-between p-3 rounded-2xl border border-slate-200/90 hover:border-emerald-400 hover:bg-emerald-50/40 transition text-left cursor-pointer group bg-slate-50/50">
                    <div class="flex items-center gap-3">
                        <img src="https://ui-avatars.com/api/?name=Korn&background=34A853&color=fff" class="w-9 h-9 rounded-full object-cover border border-slate-200">
                        <div class="flex flex-col">
                            <span class="text-sm font-bold text-slate-800 group-hover:text-emerald-600 transition">Korn</span>
                            <span class="text-xs text-slate-500">korn.roommatch@gmail.com</span>
                        </div>
                    </div>
                    <i class="fa-solid fa-arrow-right text-xs text-slate-400 group-hover:text-emerald-600 group-hover:translate-x-1 transition-transform"></i>
                </button>
            </div>

            <!-- Custom Google Email Form -->
            <div class="pt-4 border-t border-slate-100">
                <p class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">หรือกรอกอีเมล Google ของคุณ:</p>
                <form id="google-custom-form" class="flex flex-col gap-3" onsubmit="handleGoogleCustomSubmit(event)">
                    <div>
                        <div class="relative">
                            <span class="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 text-xs"><i class="fa-solid fa-envelope"></i></span>
                            <input type="email" id="google-custom-email" required placeholder="your.name@gmail.com" class="w-full bg-slate-50 border border-slate-200 text-slate-800 rounded-xl px-3 py-2.5 pl-9 text-xs sm:text-sm focus:outline-none focus:border-blue-500 focus:bg-white focus:ring-2 focus:ring-blue-100 transition">
                        </div>
                    </div>
                    <div>
                        <div class="relative">
                            <span class="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 text-xs"><i class="fa-solid fa-user"></i></span>
                            <input type="text" id="google-custom-name" placeholder="ชื่อที่ต้องการแสดง (ไม่บังคับ)" class="w-full bg-slate-50 border border-slate-200 text-slate-800 rounded-xl px-3 py-2.5 pl-9 text-xs sm:text-sm focus:outline-none focus:border-blue-500 focus:bg-white focus:ring-2 focus:ring-blue-100 transition">
                        </div>
                    </div>
                    <button type="submit" id="btn-submit-google-custom" class="w-full bg-slate-900 hover:bg-slate-800 text-white rounded-xl py-2.5 px-4 font-semibold text-xs sm:text-sm transition flex justify-center items-center gap-2 border-none cursor-pointer mt-1">
                        <span>ดำเนินการต่อด้วยบัญชีนี้</span>
                        <i class="fa-solid fa-arrow-right text-xs"></i>
                    </button>
                </form>
            </div>

            <!-- Optional Real Google Client ID Setting -->
            <div class="mt-4 pt-3 border-t border-slate-100 text-center">
                <button type="button" onclick="toggleGoogleClientConfig()" class="text-[11px] text-slate-400 hover:text-blue-600 transition bg-transparent border-none cursor-pointer underline">
                    ตั้งค่า Google OAuth Client ID สำหรับ Production
                </button>
                <div id="google-client-config-box" class="hidden mt-2 p-3 bg-slate-50 rounded-xl border border-slate-200 text-left">
                    <label class="block text-[11px] font-bold text-slate-600 mb-1">Google Client ID (OAuth 2.0)</label>
                    <div class="flex gap-2">
                        <input type="text" id="google-client-id-input" placeholder="xxxx.apps.googleusercontent.com" class="w-full bg-white border border-slate-200 rounded-lg px-2.5 py-1.5 text-xs text-slate-800 focus:outline-none focus:border-blue-500">
                        <button type="button" onclick="saveGoogleClientId()" class="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-semibold hover:bg-blue-700 transition border-none cursor-pointer flex-shrink-0">บันทึก</button>
                    </div>
                </div>
            </div>

        </div>
    `;
    document.body.appendChild(modal);

    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeGoogleModal();
    });

    return modal;
}

function startGoogleAuth(source = 'login') {
    currentGoogleAuthSource = source;
    
    // ดึง Role กรณีอยู่หน้าสมัครสมาชิก
    let role = 'customer';
    if (source === 'register') {
        const roleEl = document.getElementById('reg-role');
        if (roleEl) role = roleEl.value;
    }

    // หากมีการตั้งค่า Google Client ID จริง และ Google Identity Services พร้อมใช้งาน
    const configuredClientId = window.GOOGLE_CLIENT_ID || localStorage.getItem('google_client_id');
    if (configuredClientId) {
        if (window.google?.accounts?.oauth2) {
            try {
                const tokenClient = google.accounts.oauth2.initTokenClient({
                    client_id: configuredClientId,
                    scope: 'email profile openid',
                    callback: async (tokenResponse) => {
                        if (tokenResponse && tokenResponse.access_token) {
                            try {
                                const userInfoRes = await fetch('https://www.googleapis.com/oauth2/v3/userinfo', {
                                    headers: { Authorization: `Bearer ${tokenResponse.access_token}` }
                                });
                                const profile = await userInfoRes.json();
                                await submitGoogleAuth({
                                    email: profile.email,
                                    name: profile.name,
                                    picture: profile.picture,
                                    google_id: profile.sub,
                                    role: role
                                });
                            } catch (err) {
                                showAlert('ไม่สามารถดึงข้อมูลโปรไฟล์จาก Google ได้', 'error');
                            }
                        }
                    }
                });
                tokenClient.requestAccessToken({ prompt: 'select_account' });
                return;
            } catch (e) {
                console.warn('Google OAuth popup failed, falling back:', e);
            }
        } else if (window.google?.accounts?.id) {
            try {
                google.accounts.id.initialize({
                    client_id: configuredClientId,
                    callback: async (response) => {
                        await submitGoogleAuth({
                            credential: response.credential,
                            role: role
                        });
                    }
                });
                google.accounts.id.prompt();
                return;
            } catch (e) {
                console.warn('Google One Tap prompt failed, falling back to modal:', e);
            }
        }
    }

    // เปิดหน้าต่าง Modal ตัวเลือกบัญชี Google
    openGoogleModal(source, role);
}

function openGoogleModal(source = 'login', role = 'customer') {
    const modal = ensureGoogleModal();
    
    // ปิด Login modal ถ้าเปิดอยู่
    if (typeof closeLoginModal === 'function') {
        closeLoginModal();
    }

    const titleEl = document.getElementById('google-modal-title');
    const roleTextEl = document.getElementById('google-modal-role-text');
    const clientIdInput = document.getElementById('google-client-id-input');

    if (titleEl) {
        titleEl.textContent = source === 'register' ? 'สมัครสมาชิกด้วย Google' : 'ลงชื่อเข้าใช้ด้วย Google';
    }
    if (roleTextEl) {
        roleTextEl.textContent = role === 'seller' ? 'บทบาท: ร้านค้า (Seller)' : 'บทบาท: ผู้ใช้งานทั่วไป (Customer)';
    }
    if (clientIdInput) {
        clientIdInput.value = localStorage.getItem('google_client_id') || '';
    }

    modal.classList.remove('hidden');
    modal.classList.add('flex');
    requestAnimationFrame(() => {
        const card = modal.querySelector('.google-modal-card');
        if (card) {
            card.classList.remove('scale-95', 'opacity-0');
            card.classList.add('scale-100', 'opacity-100');
        }
    });
}

function closeGoogleModal() {
    const modal = document.getElementById('google-auth-modal');
    if (!modal) return;
    const card = modal.querySelector('.google-modal-card');
    if (card) {
        card.classList.remove('scale-100', 'opacity-100');
        card.classList.add('scale-95', 'opacity-0');
    }
    setTimeout(() => {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }, 200);
}

async function selectQuickGoogleAccount(email, name, picture) {
    let role = 'customer';
    if (currentGoogleAuthSource === 'register') {
        const roleEl = document.getElementById('reg-role');
        if (roleEl) role = roleEl.value;
    }

    await submitGoogleAuth({
        email: email,
        name: name,
        picture: picture,
        role: role
    });
}

async function handleGoogleCustomSubmit(e) {
    e.preventDefault();
    const email = document.getElementById('google-custom-email')?.value?.trim();
    const name = document.getElementById('google-custom-name')?.value?.trim();
    if (!email) return;

    let role = 'customer';
    if (currentGoogleAuthSource === 'register') {
        const roleEl = document.getElementById('reg-role');
        if (roleEl) role = roleEl.value;
    }

    await submitGoogleAuth({
        email: email,
        name: name || email.split('@')[0],
        picture: `https://ui-avatars.com/api/?name=${encodeURIComponent(name || email.split('@')[0])}&background=4285F4&color=fff`,
        role: role
    });
}

async function submitGoogleAuth(payload) {
    try {
        const submitBtn = document.getElementById('btn-submit-google-custom');
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> <span>กำลังเชื่อมต่อ...</span>';
        }

        const data = await apiFetch('/auth/google', {
            method: 'POST',
            body: JSON.stringify(payload)
        });

        Session.save(data.user);
        showAlert(data.message || 'เข้าสู่ระบบด้วย Google สำเร็จ!', 'success');
        
        // ปิด Modal ทั้งหมดทันที
        closeGoogleModal();
        if (typeof closeLoginModal === 'function') {
            closeLoginModal();
        } else {
            const loginModal = document.getElementById('login-modal');
            if (loginModal) {
                loginModal.classList.add('hidden');
                loginModal.classList.remove('flex');
            }
        }

        // ล้าง ?action=login ออกจาก URL เพื่อไม่ให้เปิด Modal ซ้ำ
        if (window.history.replaceState) {
            window.history.replaceState(null, '', window.location.pathname);
        }

        updateNavbar();

        // รีเฟรชหน้าจอเพื่อแสดงผลสถานะการเข้าสู่ระบบอย่างสมบูรณ์
        setTimeout(() => {
            if (window.location.pathname.includes('register.html')) {
                window.location.href = 'index.html';
            } else {
                window.location.reload();
            }
        }, 500);

    } catch (err) {
        showAlert(err.message || 'เข้าสู่ระบบด้วย Google ไม่สำเร็จ', 'error');
        const submitBtn = document.getElementById('btn-submit-google-custom');
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<span>ดำเนินการต่อด้วยบัญชีนี้</span> <i class="fa-solid fa-arrow-right text-xs"></i>';
        }
    }
}

function toggleGoogleClientConfig() {
    const box = document.getElementById('google-client-config-box');
    if (box) box.classList.toggle('hidden');
}

function saveGoogleClientId() {
    const input = document.getElementById('google-client-id-input');
    const val = input ? input.value.trim() : '';
    if (val) {
        localStorage.setItem('google_client_id', val);
        showAlert('บันทึก Google Client ID แล้ว', 'success');
    } else {
        localStorage.removeItem('google_client_id');
        showAlert('ลบการตั้งค่า Google Client ID แล้ว', 'info');
    }
    toggleGoogleClientConfig();
}

// Export functions to global window scope
window.startGoogleAuth = startGoogleAuth;
window.openGoogleModal = openGoogleModal;
window.closeGoogleModal = closeGoogleModal;
window.selectQuickGoogleAccount = selectQuickGoogleAccount;
window.handleGoogleCustomSubmit = handleGoogleCustomSubmit;
window.toggleGoogleClientConfig = toggleGoogleClientConfig;
window.saveGoogleClientId = saveGoogleClientId;